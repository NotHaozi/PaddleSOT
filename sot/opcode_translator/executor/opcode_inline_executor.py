from __future__ import annotations

import builtins
import inspect
from typing import TYPE_CHECKING

from ...utils import BreakGraphError, log
from .guard import StringifyExpression, union_free_vars
from .opcode_executor import OpcodeExecutorBase, Stop
from .tracker import BuiltinTracker, ConstTracker, DummyTracker, Tracker
from .variables import (
    ClosureFunctionVariable,
    DictIterVariable,
    EnumerateVariable,
    IterVariable,
    SequenceIterVariable,
)

if TYPE_CHECKING:
    from .pycode_generator import PyCodeGen
    from .variables import FunctionVariable


class FunctionGlobalTracker(Tracker):
    def __init__(self, fn: FunctionVariable, name: str):
        super().__init__([fn])
        self.fn = fn
        self.name = name

    def gen_instructions(self, codegen: PyCodeGen):
        self.fn.tracker.gen_instructions(codegen)
        codegen.gen_load_attr("__globals__")
        codegen.gen_load_const(self.name)
        codegen.gen_subscribe()

    def trace_value_from_frame(self):
        fn_tracer = self.fn.tracker.trace_value_from_frame()
        return StringifyExpression(
            f"{fn_tracer.expr}.__globals__['{self.name}']",
            union_free_vars(fn_tracer.free_vars),
        )

    def __repr__(self) -> str:
        return f"FunctionGlobalTracker(fn={self.fn}, name={self.name})"


class FunctionClosureTracker(Tracker):
    def __init__(self, fn: FunctionVariable, idx: int):
        super().__init__([fn])
        self.fn = fn
        self.idx = idx

    def gen_instructions(self, codegen: PyCodeGen):
        self.fn.tracker.gen_instructions(codegen)
        codegen.gen_load_attr("__closure__")
        codegen.gen_load_const(self.idx)
        codegen.gen_subscribe()
        codegen.gen_load_attr("cell_contents")

    def trace_value_from_frame(self):
        fn_tracer = self.fn.tracker.trace_value_from_frame()
        return StringifyExpression(
            f"{fn_tracer.expr}.__closure__[{self.idx}].cell_contents",
            union_free_vars(fn_tracer.free_vars),
        )

    def __repr__(self) -> str:
        return f"FunctionClosureTracker(fn={self.fn}, idx={self.idx})"


class OpcodeInlineExecutor(OpcodeExecutorBase):
    def __init__(self, fn_variable, *args, **kwargs):
        self._fn_var = fn_variable
        self.return_value = None
        if isinstance(self._fn_var, ClosureFunctionVariable):
            super().__init__(fn_variable.code, fn_variable.graph)
            self._closure = fn_variable.closure
            self._locals = fn_variable.locals
        else:
            self._fn_value = fn_variable.value
            super().__init__(fn_variable.get_code(), fn_variable.graph)
        self._name = "Inline"
        self._prepare_locals(*args, **kwargs)
        self._prepare_closure()
        # TODO: consider generator.

    def _prepare_locals(self, *args, **kwargs):
        from .variables import VariableBase, VariableFactory

        if isinstance(self._fn_var, ClosureFunctionVariable):
            for i in range(self._fn_var.code.co_argcount):
                name = self._fn_var.code.co_varnames[i]
                # Supplementing from default parameters
                if len(args) <= i:
                    value = self._fn_var.argdefs[len(args) - i]
                else:
                    value = args[i]
                # Convert args to Variable
                if not isinstance(value, VariableBase):
                    tracker = ConstTracker(value)
                else:
                    tracker = value.tracker
                value = VariableFactory.from_value(value, self._graph, tracker)
                self._locals[name] = value
            if (
                self._fn_var.code.co_argcount == 0
                and len(self._fn_var.code.co_varnames) == 2
            ):
                self._locals[
                    self._fn_var.code.co_varnames[0]
                ] = VariableFactory.from_value(
                    args, self._graph, DummyTracker(args)
                )
                self._locals[
                    self._fn_var.code.co_varnames[1]
                ] = VariableFactory.from_value(
                    kwargs, self._graph, DummyTracker(kwargs)
                )

        else:
            sig = inspect.signature(self._fn_value)
            bound_args = sig.bind(*args, **kwargs)
            bound_args.apply_defaults()
            for name, value in bound_args.arguments.items():
                assert name in sig.parameters
                # Convert varargs and kwargs to Variable
                if (
                    sig.parameters[name].kind
                    == inspect.Parameter.VAR_POSITIONAL
                ):
                    tracker = DummyTracker(value)
                elif sig.parameters[name].kind == inspect.Parameter.VAR_KEYWORD:
                    tracker = DummyTracker(list(value.values()))
                # Convert default args to Variable
                elif not isinstance(value, VariableBase):
                    tracker = ConstTracker(value)
                else:
                    tracker = value.tracker
                value = VariableFactory.from_value(value, self._graph, tracker)
                self._locals[name] = value

        log(
            5, f"[INLINE CALL] {self._code.co_name} with locals: ", self._locals
        )

    def _prepare_closure(self):
        from .variables import VariableFactory

        closure_vars = []
        for idx, cell in enumerate(self._closure):
            assert hasattr(self, "_fn_var")
            var = VariableFactory.from_value(
                cell, self._graph, FunctionClosureTracker(self._fn_var, idx)
            )
            closure_vars.append(var)
        self._closure = closure_vars

    def _prepare_virtual_env(self):
        # prepare globals
        from .variables import VariableFactory

        if isinstance(self._fn_var, ClosureFunctionVariable):
            globals_items = self._fn_var.globals.items()
        else:
            globals_items = self._fn_value.__globals__.items()

        for name, value in globals_items:
            self._globals[name] = VariableFactory.from_value(
                value, self._graph, FunctionGlobalTracker(self._fn_var, name)
            )

        # prepare builtins
        for name, value in builtins.__dict__.items():
            self._builtins[name] = VariableFactory.from_value(
                value, self._graph, BuiltinTracker(name)
            )

        # prepare consts
        for value in self._code.co_consts:
            self._co_consts.append(
                VariableFactory.from_value(
                    value, self._graph, ConstTracker(value)
                )
            )

    def inline_call(self):
        self.run()
        return self.return_value

    def RETURN_VALUE(self, instr):
        self.return_value = self.pop()
        return Stop()

    def _break_graph_in_jump(self, result, instr):
        raise BreakGraphError("_break_graph_in_jump.")

    def _create_resume_fn(self, index, stack_size=0):
        raise BreakGraphError("_create_resume_fn.")

    def FOR_ITER(self, instr):
        iterator = self.peek()
        assert isinstance(iterator, IterVariable)
        # breakpoint()
        # simplely get next
        if isinstance(
            iterator,
            (SequenceIterVariable, DictIterVariable, EnumerateVariable),
        ):
            try:
                self.push(iterator.next())
            except StopIteration:
                self.pop()
                self._lasti = self.indexof(instr.jump_to)

        else:
            raise BreakGraphError("For loop fallback.")
