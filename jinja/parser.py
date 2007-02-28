# -*- coding: utf-8 -*-
"""
    jinja.parser
    ~~~~~~~~~~~~

    Implements the template parser.

    :copyright: 2006 by Armin Ronacher.
    :license: BSD, see LICENSE for more details.
"""
import re
from compiler import ast, parse
from compiler.misc import set_filename
from jinja import nodes
from jinja.datastructure import TokenStream
from jinja.exceptions import TemplateSyntaxError


# callback functions for the subparse method
end_of_block = lambda p, t, d: t == 'block_end'
end_of_variable = lambda p, t, d: t == 'variable_end'
switch_for = lambda p, t, d: t == 'name' and d in ('else', 'endfor')
end_of_for = lambda p, t, d: t == 'name' and d == 'endfor'
switch_if = lambda p, t, d: t == 'name' and d in ('else', 'elif', 'endif')
end_of_if = lambda p, t, d: t == 'name' and d == 'endif'
end_of_macro = lambda p, t, d: t == 'name' and d == 'endmacro'
end_of_block_tag = lambda p, t, d: t == 'name' and d == 'endblock'


class Parser(object):
    """
    The template parser class.

    Transforms sourcecode into an abstract syntax tree.
    """

    def __init__(self, environment, source, filename=None):
        self.environment = environment
        if isinstance(source, str):
            source = source.decode(environment.template_charset, 'ignore')
        self.source = source
        self.filename = filename
        self.tokenstream = environment.lexer.tokenize(source)

        self.extends = None
        self.blocks = {}

        self.directives = {
            'for':          self.handle_for_directive,
            'if':           self.handle_if_directive,
            'cycle':        self.handle_cycle_directive,
            'print':        self.handle_print_directive,
            'macro':        self.handle_macro_directive,
            'block':        self.handle_block_directive,
            'extends':      self.handle_extends_directive
        }

    def handle_for_directive(self, lineno, gen):
        """
        Handle a for directive and return a ForLoop node
        """
        #XXX: maybe we could make the "recurse" part optional by using
        #     a static analysis later.
        recursive = []
        def wrapgen():
            """Wrap the generator to check if we have a recursive for loop."""
            for token in gen:
                if token[1:] == ('name', 'recursive'):
                    try:
                        item = gen.next()
                    except StopIteration:
                        recursive.append(True)
                        return
                    yield token
                    yield item
                else:
                    yield token
        ast = self.parse_python(lineno, wrapgen(), 'for %s:pass')
        body = self.subparse(switch_for)

        # do we have an else section?
        if self.tokenstream.next()[2] == 'else':
            self.close_remaining_block()
            else_ = self.subparse(end_of_for, True)
        else:
            else_ = None
        self.close_remaining_block()

        return nodes.ForLoop(lineno, ast.assign, ast.list, body, else_, bool(recursive))

    def handle_if_directive(self, lineno, gen):
        """
        Handle if/else blocks.
        """
        ast = self.parse_python(lineno, gen, 'if %s:pass')
        tests = [(ast.tests[0][0], self.subparse(switch_if))]

        # do we have an else section?
        while True:
            lineno, token, needle = self.tokenstream.next()
            if needle == 'else':
                self.close_remaining_block()
                else_ = self.subparse(end_of_if, True)
                break
            elif needle == 'elif':
                gen = self.tokenstream.fetch_until(end_of_block, True)
                ast = self.parse_python(lineno, gen, 'if %s:pass')
                tests.append((ast.tests[0][0], self.subparse(switch_if)))
            else:
                else_ = None
                break
        self.close_remaining_block()

        return nodes.IfCondition(lineno, tests, else_)

    def handle_cycle_directive(self, lineno, gen):
        """
        Handle {% cycle foo, bar, baz %}.
        """
        ast = self.parse_python(lineno, gen, '_cycle((%s))')
        # ast is something like Discard(CallFunc(Name('_cycle'), ...))
        # skip that.
        return nodes.Cycle(lineno, ast.expr.args[0])

    def handle_print_directive(self, lineno, gen):
        """
        Handle {{ foo }} and {% print foo %}.
        """
        ast = self.parse_python(lineno, gen, 'print_(%s)')
        # ast is something like Discard(CallFunc(Name('print_'), ...))
        # so just use the args
        arguments = ast.expr.args
        # we only accept one argument
        if len(arguments) != 1:
            raise TemplateSyntaxError('invalid argument count for print; '
                                      'print requires exactly one argument, '
                                      'got %d.' % len(arguments), lineno)
        return nodes.Print(lineno, arguments[0])

    def handle_macro_directive(self, lineno, gen):
        """
        Handle {% macro foo(bar, baz) %}.
        """
        try:
            macro_name = gen.next()
        except StopIteration:
            raise TemplateSyntaxError('macro requires a name', lineno)
        if macro_name[1] != 'name':
            raise TemplateSyntaxError('expected \'name\', got %r' %
                                      macro_name[1], lineno)
        ast = self.parse_python(lineno, gen, 'def %s(%%s):pass' % str(macro_name[2]))
        body = self.subparse(end_of_macro, True)
        self.close_remaining_block()

        if ast.varargs or ast.kwargs:
            raise TemplateSyntaxError('variable length macro signature '
                                      'not allowed.', lineno)
        defaults = [None] * (len(ast.argnames) - len(ast.defaults)) + ast.defaults
        return nodes.Macro(lineno, ast.name, zip(ast.argnames, defaults), body)

    def handle_block_directive(self, lineno, gen):
        """
        Handle block directives used for inheritance.
        """
        tokens = list(gen)
        if not tokens:
            raise TemplateSyntaxError('block requires a name', lineno)
        block_name = tokens.pop(0)
        if block_name[1] != 'name':
            raise TemplateSyntaxError('expected \'name\', got %r' %
                                      block_name[1], lineno)
        if tokens:
            print tokens
            raise TemplateSyntaxError('block got too many arguments, '
                                      'requires one.', lineno)

        if block_name[2] in self.blocks:
            raise TemplateSyntaxError('block %r defined twice' %
                                      block_name[2], lineno)

        body = self.subparse(end_of_block_tag, True)
        self.close_remaining_block()
        rv = nodes.Block(lineno, block_name[2], body)
        self.blocks[block_name[2]] = rv
        return rv

    def handle_extends_directive(self, lineno, gen):
        """
        Handle extends directive used for inheritance.
        """
        tokens = list(gen)
        if len(tokens) != 1 or tokens[0][1] != 'string':
            raise TemplateSyntaxError('extends requires a string', lineno)
        if self.extends is not None:
            raise TemplateSyntaxError('extends called twice', lineno)
        self.extends = nodes.Extends(lineno, tokens[0][2][1:-1])

    def parse_python(self, lineno, gen, template='%s'):
        """
        Convert the passed generator into a flat string representing
        python sourcecode and return an ast node or raise a
        TemplateSyntaxError.
        """
        tokens = []
        for t_lineno, t_token, t_data in gen:
            if t_token == 'string':
                tokens.append('u' + t_data)
            else:
                tokens.append(t_data)
        source = '\xef\xbb\xbf' + (template % (u' '.join(tokens)).encode('utf-8'))
        try:
            ast = parse(source, 'exec')
        except SyntaxError, e:
            raise TemplateSyntaxError('invalid syntax', lineno + e.lineno)
        assert len(ast.node.nodes) == 1, 'get %d nodes, 1 expected' % len(ast.node.nodes)
        result = ast.node.nodes[0]
        nodes.inc_lineno(lineno, result)
        return result

    def parse(self):
        """
        Parse the template and return a Template node.
        """
        body = self.subparse(None)
        return nodes.Template(self.filename, body, self.blocks, self.extends)

    def subparse(self, test, drop_needle=False):
        """
        Helper function used to parse the sourcecode until the test
        function which is passed a tuple in the form (lineno, token, data)
        returns True. In that case the current token is pushed back to
        the tokenstream and the generator ends.

        The test function is only called for the first token after a
        block tag. Variable tags are *not* aliases for {% print %} in
        that case.

        If drop_needle is True the needle_token is removed from the tokenstream.
        """
        def finish():
            """Helper function to remove unused nodelists."""
            if len(result) == 1:
                return result[0]
            return result

        lineno = self.tokenstream.last[0]
        result = nodes.NodeList(lineno)
        for lineno, token, data in self.tokenstream:
            # this token marks the begin or a variable section.
            # parse everything till the end of it.
            if token == 'variable_begin':
                gen = self.tokenstream.fetch_until(end_of_variable, True)
                result.append(self.directives['print'](lineno, gen))

            # this token marks the start of a block. like for variables
            # just parse everything until the end of the block
            elif token == 'block_begin':
                gen = self.tokenstream.fetch_until(end_of_block, True)
                try:
                    lineno, token, data = gen.next()
                except StopIteration:
                    raise TemplateSyntaxError('unexpected end of block', lineno)

                # first token *must* be a name token
                if token != 'name':
                    raise TemplateSyntaxError('unexpected %r token (%r)' % (
                                              token, data), lineno)

                # if a test function is passed to subparse we check if we
                # reached the end of such a requested block.
                if test is not None and test(lineno, token, data):
                    if not drop_needle:
                        self.tokenstream.push(lineno, token, data)
                    return finish()

                # the first token tells us which directive we want to call.
                # if if doesn't match any existing directive it's like a
                # template syntax error.
                if data in self.directives:
                    node = self.directives[data](lineno, gen)
                else:
                    raise TemplateSyntaxError('unknown directive %r' % data, lineno)
                # some tags like the extends tag do not output nodes.
                # so just skip that.
                if node is not None:
                    result.append(node)

            # here the only token we should get is "data". all other
            # tokens just exist in block or variable sections. (if the
            # tokenizer is not brocken)
            elif token == 'data':
                result.append(nodes.Text(lineno, data))

            # so this should be unreachable code
            else:
                raise AssertionError('unexpected token %r (%r)' % (token, data))

        # still here and a test function is provided? raise and error
        if test is not None:
            raise TemplateSyntaxError('unexpected end of template', lineno)
        return finish()

    def close_remaining_block(self):
        """
        If we opened a block tag because one of our tags requires an end
        tag we can use this method to drop the rest of the block from
        the stream. If the next token isn't the block end we throw an
        error.
        """
        lineno = self.tokenstream.last[0]
        try:
            lineno, token, data = self.tokenstream.next()
        except StopIteration:
            raise TemplateSyntaxError('missing closing tag', lineno)
        if token != 'block_end':
            raise TemplateSyntaxError('expected close tag, found %r' % token, lineno)
