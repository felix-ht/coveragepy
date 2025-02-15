# Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0
# For details: https://github.com/nedbat/coveragepy/blob/master/NOTICE.txt

"""Raw data collector for coverage.py."""

import atexit
import dis
import sys

from coverage import env

# We need the YIELD_VALUE opcode below, in a comparison-friendly form.
RESUME = dis.opmap.get('RESUME')
RETURN_VALUE = dis.opmap['RETURN_VALUE']
if RESUME is None:
    YIELD_VALUE = dis.opmap['YIELD_VALUE']
    YIELD_FROM = dis.opmap['YIELD_FROM']
    YIELD_FROM_OFFSET = 0 if env.PYPY else 2

# When running meta-coverage, this file can try to trace itself, which confuses
# everything.  Don't trace ourselves.

THIS_FILE = __file__.rstrip("co")

class PyTracer:
    """Python implementation of the raw data tracer."""

    # Because of poor implementations of trace-function-manipulating tools,
    # the Python trace function must be kept very simple.  In particular, there
    # must be only one function ever set as the trace function, both through
    # sys.settrace, and as the return value from the trace function.  Put
    # another way, the trace function must always return itself.  It cannot
    # swap in other functions, or return None to avoid tracing a particular
    # frame.
    #
    # The trace manipulator that introduced this restriction is DecoratorTools,
    # which sets a trace function, and then later restores the pre-existing one
    # by calling sys.settrace with a function it found in the current frame.
    #
    # Systems that use DecoratorTools (or similar trace manipulations) must use
    # PyTracer to get accurate results.  The command-line --timid argument is
    # used to force the use of this tracer.

    def __init__(self):
        # Attributes set from the collector:
        self.data = None
        self.trace_arcs = False
        self.should_trace = None
        self.should_trace_cache = None
        self.should_start_context = None
        self.warn = None
        # The threading module to use, if any.
        self.threading = None

        self.cur_file_data = None
        self.last_line = 0          # int, but uninitialized.
        self.cur_file_name = None
        self.context = None
        self.started_context = False

        self.data_stack = []
        self.thread = None
        self.stopped = False
        self._activity = False

        self.in_atexit = False
        # On exit, self.in_atexit = True
        atexit.register(setattr, self, 'in_atexit', True)

    def __repr__(self):
        return "<PyTracer at 0x{:x}: {} lines in {} files>".format(
            id(self),
            sum(len(v) for v in self.data.values()),
            len(self.data),
        )

    def log(self, marker, *args):
        """For hard-core logging of what this tracer is doing."""
        with open("/tmp/debug_trace.txt", "a") as f:
            f.write("{} {}[{}]".format(
                marker,
                id(self),
                len(self.data_stack),
            ))
            if 0:   # if you want thread ids..
                f.write(".{:x}.{:x}".format(
                    self.thread.ident,
                    self.threading.current_thread().ident,
                ))
            f.write(" {}".format(" ".join(map(str, args))))
            if 0:   # if you want callers..
                f.write(" | ")
                stack = " / ".join(
                    (fname or "???").rpartition("/")[-1]
                    for _, fname, _, _ in self.data_stack
                )
                f.write(stack)
            f.write("\n")

    def _trace(self, frame, event, arg_unused):
        """The trace function passed to sys.settrace."""

        if THIS_FILE in frame.f_code.co_filename:
            return None

        #self.log(":", frame.f_code.co_filename, frame.f_lineno, frame.f_code.co_name + "()", event)

        if (self.stopped and sys.gettrace() == self._trace):    # pylint: disable=comparison-with-callable
            # The PyTrace.stop() method has been called, possibly by another
            # thread, let's deactivate ourselves now.
            if 0:
                self.log("---\nX", frame.f_code.co_filename, frame.f_lineno)
                f = frame
                while f:
                    self.log(">", f.f_code.co_filename, f.f_lineno, f.f_code.co_name, f.f_trace)
                    f = f.f_back
            sys.settrace(None)
            self.cur_file_data, self.cur_file_name, self.last_line, self.started_context = (
                self.data_stack.pop()
            )
            return None

        # if event != 'call' and frame.f_code.co_filename != self.cur_file_name:
        #     self.log("---\n*", frame.f_code.co_filename, self.cur_file_name, frame.f_lineno)

        if event == 'call':
            # Should we start a new context?
            if self.should_start_context and self.context is None:
                context_maybe = self.should_start_context(frame)
                if context_maybe is not None:
                    self.context = context_maybe
                    self.started_context = True
                    self.switch_context(self.context)
                else:
                    self.started_context = False
            else:
                self.started_context = False

            # Entering a new frame.  Decide if we should trace in this file.
            self._activity = True
            self.data_stack.append(
                (
                    self.cur_file_data,
                    self.cur_file_name,
                    self.last_line,
                    self.started_context,
                )
            )
            filename = frame.f_code.co_filename
            self.cur_file_name = filename
            disp = self.should_trace_cache.get(filename)
            if disp is None:
                disp = self.should_trace(filename, frame)
                self.should_trace_cache[filename] = disp

            self.cur_file_data = None
            if disp.trace:
                tracename = disp.source_filename
                if tracename not in self.data:
                    self.data[tracename] = set()
                self.cur_file_data = self.data[tracename]
            else:
                frame.f_trace_lines = False

            # The call event is really a "start frame" event, and happens for
            # function calls and re-entering generators.  The f_lasti field is
            # -1 for calls, and a real offset for generators.  Use <0 as the
            # line number for calls, and the real line number for generators.
            if RESUME is not None:
                # The current opcode is guaranteed to be RESUME. The argument
                # determines what kind of resume it is.
                oparg = frame.f_code.co_code[frame.f_lasti + 1]
                real_call = (oparg == 0)
            else:
                real_call = (getattr(frame, 'f_lasti', -1) < 0)
            if real_call:
                self.last_line = -frame.f_code.co_firstlineno
            else:
                self.last_line = frame.f_lineno

        elif event == 'line':
            # Record an executed line.
            if self.cur_file_data is not None:
                lineno = frame.f_lineno

                if self.trace_arcs:
                    self.cur_file_data.add((self.last_line, lineno))
                else:
                    self.cur_file_data.add(lineno)
                self.last_line = lineno

        elif event == 'return':
            if self.trace_arcs and self.cur_file_data:
                # Record an arc leaving the function, but beware that a
                # "return" event might just mean yielding from a generator.
                code = frame.f_code.co_code
                lasti = frame.f_lasti
                if RESUME is not None:
                    if len(code) == lasti + 2:
                        # A return from the end of a code object is a real return.
                        real_return = True
                    else:
                        # it's a real return.
                        real_return = (code[lasti + 2] != RESUME)
                else:
                    if code[lasti] == RETURN_VALUE:
                        real_return = True
                    elif code[lasti] == YIELD_VALUE:
                        real_return = False
                    elif len(code) <= lasti + YIELD_FROM_OFFSET:
                        real_return = True
                    elif code[lasti + YIELD_FROM_OFFSET] == YIELD_FROM:
                        real_return = False
                    else:
                        real_return = True
                if real_return:
                    first = frame.f_code.co_firstlineno
                    self.cur_file_data.add((self.last_line, -first))

            # Leaving this function, pop the filename stack.
            self.cur_file_data, self.cur_file_name, self.last_line, self.started_context = (
                self.data_stack.pop()
            )
            # Leaving a context?
            if self.started_context:
                self.context = None
                self.switch_context(None)
        return self._trace

    def start(self):
        """Start this Tracer.

        Return a Python function suitable for use with sys.settrace().

        """
        self.stopped = False
        if self.threading:
            if self.thread is None:
                self.thread = self.threading.current_thread()
            else:
                if self.thread.ident != self.threading.current_thread().ident:
                    # Re-starting from a different thread!? Don't set the trace
                    # function, but we are marked as running again, so maybe it
                    # will be ok?
                    #self.log("~", "starting on different threads")
                    return self._trace

        sys.settrace(self._trace)
        return self._trace

    def stop(self):
        """Stop this Tracer."""
        # Get the active tracer callback before setting the stop flag to be
        # able to detect if the tracer was changed prior to stopping it.
        tf = sys.gettrace()

        # Set the stop flag. The actual call to sys.settrace(None) will happen
        # in the self._trace callback itself to make sure to call it from the
        # right thread.
        self.stopped = True

        if self.threading and self.thread.ident != self.threading.current_thread().ident:
            # Called on a different thread than started us: we can't unhook
            # ourselves, but we've set the flag that we should stop, so we
            # won't do any more tracing.
            #self.log("~", "stopping on different threads")
            return

        if self.warn:
            # PyPy clears the trace function before running atexit functions,
            # so don't warn if we are in atexit on PyPy and the trace function
            # has changed to None.
            dont_warn = (env.PYPY and env.PYPYVERSION >= (5, 4) and self.in_atexit and tf is None)
            if (not dont_warn) and tf != self._trace:   # pylint: disable=comparison-with-callable
                self.warn(
                    f"Trace function changed, data is likely wrong: {tf!r} != {self._trace!r}",
                    slug="trace-changed",
                )

    def activity(self):
        """Has there been any activity?"""
        return self._activity

    def reset_activity(self):
        """Reset the activity() flag."""
        self._activity = False

    def get_stats(self):
        """Return a dictionary of statistics, or None."""
        return None
