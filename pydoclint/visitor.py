import ast
from typing import List, Optional, Set

from numpydoc.docscrape import NumpyDocString, Parameter

from pydoclint.arg import Arg, ArgList
from pydoclint.method_type import MethodType
from pydoclint.utils.astTypes import FuncOrAsyncFuncDef
from pydoclint.utils.generic import (
    detectMethodType,
    generateMsgPrefix,
    getDocstring,
    isShortDocstring,
)
from pydoclint.utils.return_yield_raise import (
    hasGeneratorAsReturnAnnotation,
    hasRaiseStatements,
    hasReturnAnnotation,
    hasReturnStatements,
    hasYieldStatements,
)
from pydoclint.violation import Violation


class Visitor(ast.NodeVisitor):
    """A class to recursively visit all the nodes in a parsed module"""

    def __init__(
            self,
            checkTypeHint: bool = True,
            checkArgOrder: bool = True,
            skipCheckingShortDocstrings: bool = True,
            skipCheckingRaises: bool = False,
    ) -> None:
        self.checkTypeHint: bool = checkTypeHint
        self.checkArgOrder: bool = checkArgOrder
        self.skipCheckingShortDocstrings: bool = skipCheckingShortDocstrings
        self.skipCheckingRaises: bool = skipCheckingRaises

        self.parent: Optional[ast.AST] = None  # keep track of parent node
        self.violations: List[Violation] = []

    def visit_ClassDef(self, node: ast.ClassDef):  # noqa: D102
        currentParent = self.parent  # keep aside
        self.parent = node

        self.generic_visit(node)

        self.parent = currentParent  # restore

    def visit_FunctionDef(self, node: FuncOrAsyncFuncDef):  # noqa: D102
        parent_ = self.parent  # keep aside
        self.parent = node

        isClassConstructor: bool = node.name == '__init__' and isinstance(
            parent_, ast.ClassDef
        )

        docstring: str = getDocstring(node)

        if isClassConstructor:
            className: str = parent_.name
            if len(docstring) > 0:  # __init__() has its own docstring
                self.violations.append(
                    Violation(
                        code=301,
                        line=node.lineno,
                        msgPrefix=f'Class `{className}`:',
                    )
                )

            # Inspect class docstring instead, because that's what we care
            # about when checking the class constructor.
            docstring = getDocstring(parent_)

        argViolations: List[Violation]
        returnViolations: List[Violation]
        yieldViolations: List[Violation]
        raiseViolations: List[Violation]

        if docstring == '':
            # We don't check functions without docstrings.
            # We defer to
            # flake8-docstrings (https://github.com/PyCQA/flake8-docstrings)
            # or pydocstyle (https://www.pydocstyle.org/en/stable/)
            # to determine whether a function needs a docstring.
            argViolations = []
            returnViolations = []
            yieldViolations = []
            raiseViolations = []
        else:
            # Note: a NumpyDocString object has the following sections:
            # *  {'Signature': '', 'Summary': [''], 'Extended Summary': [],
            # *  'Parameters': [], 'Returns': [], 'Yields': [], 'Receives': [],
            # *  'Raises': [], 'Warns': [], 'Other Parameters': [],
            # *  'Attributes': [], 'Methods': [], 'See Also': [], 'Notes': [],
            # *  'Warnings': [], 'References': '', 'Examples': '', 'index': {}}
            doc: NumpyDocString = NumpyDocString(docstring)

            isShort: bool = isShortDocstring(doc)
            if self.skipCheckingShortDocstrings and isShort:
                argViolations = []
                returnViolations = []
                yieldViolations = []
                raiseViolations = []
            else:
                argViolations = self.checkArguments(node, parent_, doc)
                if docstring == '':
                    returnViolations = []
                    yieldViolations = []
                    raiseViolations = []
                else:
                    returnViolations = self.checkReturns(node, parent_, doc)
                    yieldViolations = self.checkYields(node, parent_, doc)
                    if not self.skipCheckingRaises:
                        raiseViolations = self.checkRaises(node, parent_, doc)
                    else:
                        raiseViolations = []

            if isClassConstructor:
                # Re-check return violations because the rules are
                # different for class constructors.
                returnViolations = self.checkReturnsInClassConstructor(
                    parent=parent_, nonEmptyDocStruct=doc
                )

        self.violations.extend(argViolations)
        self.violations.extend(returnViolations)
        self.violations.extend(yieldViolations)
        self.violations.extend(raiseViolations)

        self.generic_visit(node)

        self.parent = parent_  # restore

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):  # noqa: D102
        # Treat async functions similarly to regular ones
        self.visit_FunctionDef(node)

    def visit_Raise(self, node: ast.Raise):  # noqa: D102
        self.generic_visit(node)

    def checkArguments(
            self,
            node: FuncOrAsyncFuncDef,
            parent_: ast.AST,
            docstringStruct: NumpyDocString,
    ) -> List[Violation]:
        """
        Check input arguments of the function.

        Parameters
        ----------
        node : FuncOrAsyncFuncDef
            The current function node.  It can be a regular function
            or an async function.
        parent_ : ast.AST
            The parent of the current node, which can be another function,
            a class, etc.
        docstringStruct : NumpyDocString
            The parsed docstring structure.

        Returns
        -------
        List[Violation]
            A list of argument violations
        """
        argList: List[ast.arg] = list(node.args.args)

        isMethod: bool = isinstance(parent_, ast.ClassDef)
        msgPrefix: str = generateMsgPrefix(node, parent_, appendColon=True)

        if isMethod:
            mType: MethodType = detectMethodType(node)
            if mType in {MethodType.INSTANCE_METHOD, MethodType.CLASS_METHOD}:
                argList = argList[1:]  # no need to document `self` and `cls`

        docArgList: List[Parameter] = docstringStruct.get('Parameters', [])

        return self.validateDocArgs(
            docArgList=docArgList,
            actualArgs=argList,
            node=node,
            msgPrefix=msgPrefix,
        )

    def validateDocArgs(  # noqa: C901
            self,
            docArgList: List[Parameter],
            actualArgs: List[ast.arg],
            node: FuncOrAsyncFuncDef,
            msgPrefix: str,
    ) -> List[Violation]:
        """
        Validate the argument list in the docstring against the "actual"
        arguments (the argument list in the function signature).

        Parameters
        ----------
        docArgList : List[Parameter]
            The argument list from the docstring
        actualArgs : List[ast.arg]
            The argument list from the function signature
        node : FuncOrAsyncFuncDef
            The current function node
        msgPrefix : str
            The prefix to be used in the violation message

        Returns
        -------
        List[Violation]
            A list of argument violations. It can be empty.
        """
        lineNum: int = node.lineno

        v101 = Violation(code=101, line=lineNum, msgPrefix=msgPrefix)
        v102 = Violation(code=102, line=lineNum, msgPrefix=msgPrefix)
        v104 = Violation(code=104, line=lineNum, msgPrefix=msgPrefix)
        v105 = Violation(code=105, line=lineNum, msgPrefix=msgPrefix)

        docArgs = ArgList([Arg.fromNumpydocParam(_) for _ in docArgList])
        funcArgs = ArgList([Arg.fromAstArg(_) for _ in actualArgs])

        if docArgs.length() == 0 and funcArgs.length() == 0:
            return []

        violations: List[Violation] = []
        if docArgs.length() < funcArgs.length():
            violations.append(v101)

        if docArgs.length() > funcArgs.length():
            violations.append(v102)

        if not docArgs.equals(
            funcArgs,
            checkTypeHint=self.checkTypeHint,
            orderMatters=self.checkArgOrder,
        ):
            if docArgs.equals(
                funcArgs,
                checkTypeHint=self.checkTypeHint,
                orderMatters=False,
            ):
                violations.append(v104)
            elif docArgs.equals(
                funcArgs,
                checkTypeHint=False,
                orderMatters=self.checkArgOrder,
            ):
                violations.append(v105)
            elif docArgs.equals(
                funcArgs,
                checkTypeHint=False,
                orderMatters=False,
            ):
                violations.append(v104)
                violations.append(v105)
            else:
                argsInFuncNotInDoc: Set[Arg] = funcArgs.subtract(docArgs)
                argsInDocNotInFunc: Set[Arg] = docArgs.subtract(funcArgs)

                msgPostfixParts: List[str] = []
                if argsInFuncNotInDoc:
                    msgPostfixParts.append(
                        'Arguments in the function signature but not in the'
                        f' docstring: {sorted(argsInFuncNotInDoc)}.'
                    )

                if argsInDocNotInFunc:
                    msgPostfixParts.append(
                        'Arguments in the docstring but not in the function'
                        f' signature: {sorted(argsInDocNotInFunc)}.'
                    )

                violations.append(
                    Violation(
                        code=103,
                        line=lineNum,
                        msgPrefix=msgPrefix,
                        msgPostfix=' '.join(msgPostfixParts),
                    )
                )

        return violations

    @classmethod
    def checkReturns(
            cls,
            node: FuncOrAsyncFuncDef,
            parent: ast.AST,
            nonEmptyDocStruct: NumpyDocString,
    ) -> List[Violation]:
        """Check return statement & return type annotation of this function"""
        lineNum: int = node.lineno
        msgPrefix = generateMsgPrefix(node, parent, appendColon=False)

        v201 = Violation(code=201, line=lineNum, msgPrefix=msgPrefix)
        v202 = Violation(code=202, line=lineNum, msgPrefix=msgPrefix)

        hasReturnStmt: bool = hasReturnStatements(node)
        hasReturnAnno: bool = hasReturnAnnotation(node)
        hasGenAsRetAnno: bool = hasGeneratorAsReturnAnnotation(node)

        docstringHasReturnSection = bool(nonEmptyDocStruct.get('Returns'))

        violations: List[Violation] = []
        if not docstringHasReturnSection:
            if hasReturnStmt or (hasReturnAnno and not hasGenAsRetAnno):
                # If "Generator[...]" is put in the return type annotation,
                # we don't need a "Returns" section in the docstring. Instead,
                # we need a "Yields" section.
                violations.append(v201)

        if docstringHasReturnSection and not (hasReturnStmt or hasReturnAnno):
            violations.append(v202)

        return violations

    @classmethod
    def checkReturnsInClassConstructor(
            cls,
            parent: ast.ClassDef,
            nonEmptyDocStruct: NumpyDocString,
    ) -> List[Violation]:
        """Check the presence of a "Returns" section in class docstring"""
        violations: List[Violation] = []

        docstringHasReturnSection = bool(nonEmptyDocStruct.get('Returns'))

        if docstringHasReturnSection:
            violations.append(
                Violation(
                    code=302,
                    line=parent.lineno,
                    msgPrefix=f'Class `{parent.name}`:',
                )
            )

        return violations

    @classmethod
    def checkYields(
            cls,
            node: FuncOrAsyncFuncDef,
            parent: ast.AST,
            nonEmptyDocStruct: NumpyDocString,
    ) -> List[Violation]:
        """Check violations on 'yield' statements or 'Generator' annotation"""
        violations: List[Violation] = []

        lineNum: int = node.lineno
        msgPrefix = generateMsgPrefix(node, parent, appendColon=False)

        v401 = Violation(code=401, line=lineNum, msgPrefix=msgPrefix)
        v402 = Violation(code=402, line=lineNum, msgPrefix=msgPrefix)
        v403 = Violation(code=403, line=lineNum, msgPrefix=msgPrefix)

        docstringHasYieldsSection = bool(nonEmptyDocStruct.get('Yields'))
        hasYieldStmt: bool = hasYieldStatements(node)
        hasGenAsRetAnno: bool = hasGeneratorAsReturnAnnotation(node)

        if not docstringHasYieldsSection:
            if hasGenAsRetAnno:
                violations.append(v401)

            if hasYieldStmt:
                violations.append(v402)

        if docstringHasYieldsSection:
            if not hasYieldStmt and not hasGenAsRetAnno:
                violations.append(v403)

        return violations

    @classmethod
    def checkRaises(
            cls,
            node: FuncOrAsyncFuncDef,
            parent: ast.AST,
            nonEmptyDocStruct: NumpyDocString,
    ) -> List[Violation]:
        """Check violations on 'raise' statements"""
        violations: List[Violation] = []

        lineNum: int = node.lineno
        msgPrefix = generateMsgPrefix(node, parent, appendColon=False)

        v501 = Violation(code=501, line=lineNum, msgPrefix=msgPrefix)
        v502 = Violation(code=502, line=lineNum, msgPrefix=msgPrefix)

        docstringHasRaisesSection = bool(nonEmptyDocStruct.get('Raises'))
        hasRaiseStmt: bool = hasRaiseStatements(node)

        if hasRaiseStmt and not docstringHasRaisesSection:
            violations.append(v501)

        if not hasRaiseStmt and docstringHasRaisesSection:
            violations.append(v502)

        return violations