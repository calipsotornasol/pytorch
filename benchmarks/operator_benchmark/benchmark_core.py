from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import functools
import numpy as np
import timeit
import json

import benchmark_utils
from collections import namedtuple

"""Performance microbenchmarks.

This module contains core functionalities for performance microbenchmark tests.
"""

"""
This is used to store configs of tests 
An example input is: 
TestConfig(test_name='M_8_N_2_K_1', input_config='M: 8, N: 2, K: 1', 
    tag='long', run_backward=False)
"""
TestConfig = namedtuple("TestConfig", "test_name input_config tag run_backward")


BENCHMARK_TESTER = {}


def _register_test(test_case):
    """ This method is used to register test. func_name is a global unique 
    string. For PyTorch add operator with M=8, N=2, K=1, tag = long, here 
    are the values for the members in test_case:
    op.module_name: add
    framework: PyTorch
    test_config: TestConfig(test_name='M_8_N_2_K_1', input_config='M: 8, N: 2, K: 1', 
        tag='long', run_backward=False)
    func_name: addPyTorchTestConfig(test_name='M8_N2_K1', input_config='M: 8, N: 2, K: 1',
                                    tag='long', run_backward=False)
    """
    test_config = test_case.test_config
    op = test_case.op_bench
    func_name = "{}{}{}".format(op.module_name(), test_case.framework, str(test_config))
    BENCHMARK_TESTER[func_name] = test_case


class BenchmarkRunner(object):
    """BenchmarkRunner is responsible for benchmarking all the registered
    benchmark test groups.

    Attributes:
        tag_filter (str): control the benchmarks which matches the tag. 
        operator (str): only run benchmark test cases that contains
    this filter string in the test case's id.
        test_name (str): only run benchmark test cases that matches this filter,
        this is a case-sensitive substring match and it happens in
        the _keep_test method. 
    """
    def __init__(self, args):
        # TODO: consider time-bound constraints as well.
        self.args = args
        self.iters = 100
        self.has_explicit_iteration_count = False
        self.multiplier = 2
        self.predefined_minimum_secs = 4
        self.max_iters = 1e6
        if self.args.iterations:
            self.has_explicit_iteration_count = True
            self.iters = self.args.iterations

    def _print_header(self):
        DASH_LINE = '-' * 40
        print("# {}\n"
              "# PyTorch/Caffe2 Operator Micro-benchmarks\n"
              "# {}\n"
              "# Tag : {}\n".format(DASH_LINE, DASH_LINE, self.args.tag_filter))
        if self.args.list_ops:
            print("# List of Operators to run:")
            if self.args.operator is None:
                ops = set(test_case.op_bench.module_name()
                          for _, test_case in BENCHMARK_TESTER.items())
                for op in ops: 
                    print("# {}".format(op))
            else:
                print("# {}".format(self.args.operator))

    def _print_perf_result(self, full_test_id, reported_run_time_us, test_case):
        if self.args.ai_pep_format:
            # Output for AI-PEP
            print("Caffe2Observer " + json.dumps(
                {
                    "type": "NET",
                    "metric": full_test_id,
                    "unit": "us",
                    "value": str(reported_run_time_us),
                }
            ))
        else:
            # FIXME: change the print format here 
            output = "# Name: {}\n" \
                     "# Input: {}\n" \
                     "{} Execution Time (us) : {:.3f}\n"
            if test_case.framework == "PyTorch":
                # FIXME: add JIT 
                output = "# Mode: Eager\n" + output
            print(output.format(
                test_case.test_config.test_name,
                test_case.test_config.input_config,
                "Backward" if test_case.test_config.run_backward else "Forward", reported_run_time_us))

    def _predict_num_iter_needed(self, i):
        return (i * self.multiplier)

    def _iteration_result_is_significant(self, iters, run_time_sec, curr_test_total_time, has_explicit_iteration_count):
        """ This function decides whether the measured time can be reported based on the 
        following conditions: 1) the number of iterations is larger than the max_iters.
        2) the execution time is larger than the predefined minimum_time
        3) the execution time is larger than user defined minimum_time 
        """
        return ((iters > self.max_iters or
                run_time_sec > self.predefined_minimum_secs or 
                has_explicit_iteration_count) and
                curr_test_total_time > self.args.min_time_per_test)

    def _launch_forward(self, test_case, iters):
        """ Use Python's timeit module to measure execution time (unit: second).
        """
        if test_case.framework == "PyTorch":
            test_case.op_bench.generate_jit_forward_graph(iters)

        forward_time = timeit.timeit(functools.partial(test_case.run_forward, iters), number=1)
        return forward_time

    def _launch_backward(self, test_case, iters):
        """ This function runs forward path of an op to get an output. Then the backward path is executed 
        and the execution time is reported
        """
        if test_case.framework == "PyTorch":
            # We only need to get the output for backward path, so there is no need to use JIT here 
            test_case.run_forward_eager()
            test_case.loss_func()
        else:
            test_case.run_forward(1)
        backward_time = timeit.timeit(functools.partial(test_case.run_backward, iters), number=1)
        return backward_time

    def _measure_time(self, launch_test, test_case, iters):
        """
        This function execute the operator for <iters> iterations then look at the time. 
        If it's not significant, the number of iterations will be increased before rerun. 
        The execution stops when the time becomes significant.
        """
        curr_test_total_time = 0
        while True:
            run_time_sec = launch_test(test_case, iters)
            curr_test_total_time += run_time_sec
            # Analyze time after each run to decide if the result is stable
            results_are_significant = self._iteration_result_is_significant(
                iters, run_time_sec, curr_test_total_time, self.has_explicit_iteration_count)

            if results_are_significant:
                break

            # Re-estimate the hopefully-sufficient
            # iteration count, and run the benchmark again...
            iters = self._predict_num_iter_needed(iters)

        reported_run_time_us = (1e6 * run_time_sec / iters)
        return reported_run_time_us

    def _check_keep(self, test_flag, cmd_flag):
        return (cmd_flag is None or test_flag == cmd_flag)

    def _check_keep_list(self, test_flag, cmd_flag_list):
        if (cmd_flag_list is None or 
                any(test_flag == cmd_flag for cmd_flag in cmd_flag_list)):
            return True
        return False

    def _keep_test(self, test_case):
        # TODO: consider regex matching for test filtering.
        # Currently, this is a sub-string matching.
        op_test_config = test_case.test_config

        if self.args.framework:
            frameworks = benchmark_utils.get_requested_frameworks(self.args.framework)

        # Filter framework, operator, test_name, tag, forward_only
        if (self._check_keep(op_test_config.test_name, self.args.test_name) and
            self._check_keep(op_test_config.tag, self.args.tag_filter) and
            self._check_keep(test_case.op_bench.module_name(), self.args.operator) and
            self._check_keep_list(test_case.framework, frameworks) and 
                (op_test_config.run_backward == self.args.forward_only)):
            return True

        return False

    def run(self):
        self._print_header()

        if self.args.list_ops:
            return

        for full_test_id, test_case in BENCHMARK_TESTER.items():
            op_test_config = test_case.test_config 

            if not self._keep_test(test_case):
                continue

            # To reduce variance, fix a numpy randseed to the test case,
            # so that the randomly generated input tensors remain the
            # same for each test case.
            # The random seed is limited to 32-bit because of numpy
            # requirement.
            np.random.seed(seed=hash(full_test_id) & ((1 << 32) - 1))

            print("# Benchmarking {}: {}".format(
                test_case.framework,
                test_case.op_bench.module_name()))

            if op_test_config.run_backward:
                # Warmup
                self._launch_backward(test_case, self.args.warmup_iterations)
                # Actual Execution
                reported_time = self._measure_time(self._launch_backward, test_case, self.iters)
            else: 
                # Warmup
                self._launch_forward(test_case, self.args.warmup_iterations)
                # Actual Execution
                reported_time = self._measure_time(self._launch_forward, test_case, self.iters)

            self._print_perf_result(full_test_id, reported_time, test_case)
