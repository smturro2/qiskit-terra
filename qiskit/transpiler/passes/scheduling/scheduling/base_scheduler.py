# This code is part of Qiskit.
#
# (C) Copyright IBM 2020.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Base circuit scheduling pass."""

import warnings

from typing import Dict
from qiskit.transpiler import InstructionDurations
from qiskit.transpiler.basepasses import AnalysisPass
from qiskit.transpiler.passes.scheduling.time_unit_conversion import TimeUnitConversion
from qiskit.dagcircuit import DAGOpNode, DAGCircuit
from qiskit.circuit import Delay, Gate
from qiskit.circuit.parameterexpression import ParameterExpression
from qiskit.transpiler.exceptions import TranspilerError


class BaseScheduler(AnalysisPass):
    """Base scheduler pass.

    Policy of topological node ordering in scheduling

        The DAG representation of ``QuantumCircuit`` respects the node ordering also in the
        classical register wires, though theoretically two conditional instructions
        conditioned on the same register are commute, i.e. read-access to the
        classical register doesn't change its state.

        .. parsed-literal::

            qc = QuantumCircuit(2, 1)
            qc.delay(100, 0)
            qc.x(0).c_if(0, True)
            qc.x(1).c_if(0, True)

        The scheduler SHOULD comply with above topological ordering policy of the DAG circuit.
        Accordingly, the `asap`-scheduled circuit will become

        .. parsed-literal::

                 ┌────────────────┐   ┌───┐
            q_0: ┤ Delay(100[dt]) ├───┤ X ├──────────────
                 ├────────────────┤   └─╥─┘      ┌───┐
            q_1: ┤ Delay(100[dt]) ├─────╫────────┤ X ├───
                 └────────────────┘     ║        └─╥─┘
                                   ┌────╨────┐┌────╨────┐
            c: 1/══════════════════╡ c_0=0x1 ╞╡ c_0=0x1 ╞
                                   └─────────┘└─────────┘

        Note that this scheduling might be inefficient in some cases,
        because the second conditional operation can start without waiting the delay of 100 dt.
        However, such optimization should be done by another pass,
        otherwise scheduling may break topological ordering of the original circuit.

    Realistic control flow scheduling respecting for microarcitecture

        In the dispersive QND readout scheme, qubit is measured with microwave stimulus to qubit (Q)
        followed by resonator ring-down (depopulation). This microwave signal is recorded
        in the buffer memory (B) with hardware kernel, then a discriminated (D) binary value
        is moved to the classical register (C).
        The sequence from t0 to t1 of the measure instruction interval might be modeled as follows:

        .. parsed-literal::

            Q ░▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒░
            B ░░▒▒▒▒▒▒▒▒░░░░░░░░░
            D ░░░░░░░░░░▒▒▒▒▒▒░░░
            C ░░░░░░░░░░░░░░░░▒▒░

        However, ``QuantumCircuit`` representation is not enough accurate to represent
        this model. In the circuit representation, thus ``Qubit`` is occupied by the
        stimulus microwave signal during the first half of the interval,
        and ``Clbit`` is only occupied at the very end of the interval.

        This precise model may induce weird edge case.

        .. parsed-literal::

                    ┌───┐
            q_0: ───┤ X ├──────
                    └─╥─┘   ┌─┐
            q_1: ─────╫─────┤M├
                 ┌────╨────┐└╥┘
            c: 1/╡ c_0=0x1 ╞═╩═
                 └─────────┘ 0

        In this example, user may intend to measure the state of ``q_1``, after ``XGate`` is
        applied to the ``q_0``. This is correct interpretation from viewpoint of
        the topological node ordering, i.e. x gate node come in front of the measure node.
        However, according to the measurement model above, the data in the register
        is unchanged during the stimulus, thus two nodes are simultaneously operated.
        If one `alap`-schedule this circuit, it may return following circuit.

        .. parsed-literal::

                 ┌────────────────┐   ┌───┐
            q_0: ┤ Delay(500[dt]) ├───┤ X ├──────
                 └────────────────┘   └─╥─┘   ┌─┐
            q_1: ───────────────────────╫─────┤M├
                                   ┌────╨────┐└╥┘
            c: 1/══════════════════╡ c_0=0x1 ╞═╩═
                                   └─────────┘ 0

        Note that there is no delay on ``q_1`` wire, and the measure instruction immediately
        start after t=0, while the conditional gate starts after the delay.
        It looks like the topological ordering between the nodes are flipped in the scheduled view.
        This behavior can be understood by considering the control flow model described above,

        .. parsed-literal::

            : Quantum Circuit, first-measure
            0 ░░░░░░░░░░░░▒▒▒▒▒▒░
            1 ░▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒░

            : In wire q0
            Q ░░░░░░░░░░░░░░░▒▒▒░
            C ░░░░░░░░░░░░▒▒░░░░░

            : In wire q1
            Q ░▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒░
            B ░░▒▒▒▒▒▒▒▒░░░░░░░░░
            D ░░░░░░░░░░▒▒▒▒▒▒░░░
            C ░░░░░░░░░░░░░░░░▒▒░

        Since there is no qubit register (Q0, Q1) overlap, the node ordering is determined by the
        shared classical register C. As you can see, the execution order is still
        preserved on C, i.e. read C then apply ``XGate``, finally store the measured outcome in C.
        Because ``DAGOpNode`` cannot define different durations for associated registers,
        the time ordering of two nodes is inverted anyways.

        This behavior can be controlled by ``clbit_write_latency`` and ``conditional_latency``.
        The former parameter determines the delay of the register write-access from
        the beginning of the measure instruction t0, and another parameter determines
        the delay of conditional gate operation from t0 which comes from the register read-access.
        These information might be found in the backend configuration and then should
        be copied to the pass manager property set before the pass is called.

        By default latencies, the `alap`-scheduled circuit of above example may become

        .. parsed-literal::

                    ┌───┐
            q_0: ───┤ X ├──────
                    └─╥─┘   ┌─┐
            q_1: ─────╫─────┤M├
                 ┌────╨────┐└╥┘
            c: 1/╡ c_0=0x1 ╞═╩═
                 └─────────┘ 0

        If the backend microarchitecture supports smart scheduling of the control flow, i.e.
        it may separately schedule qubit and classical register,
        insertion of the delay yields unnecessary longer total execution time.

        .. parsed-literal::
            : Quantum Circuit, first-xgate
            0 ░▒▒▒░░░░░░░░░░░░░░░
            1 ░▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒░

            : In wire q0
            Q ░▒▒▒░░░░░░░░░░░░░░░
            C ░░░░░░░░░░░░░░░░░░░ (zero latency)

            : In wire q1
            Q ░▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒░
            C ░▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒░ (zero latency, scheduled after C0 read-access)

        However this result is much more intuitive in the topological ordering view.
        If finite conditional latency is provided, for example, 30 dt, the circuit
        is scheduled as follows.

        .. parsed-literal::

                 ┌───────────────┐   ┌───┐
            q_0: ┤ Delay(30[dt]) ├───┤ X ├──────
                 ├───────────────┤   └─╥─┘   ┌─┐
            q_1: ┤ Delay(30[dt]) ├─────╫─────┤M├
                 └───────────────┘┌────╨────┐└╥┘
            c: 1/═════════════════╡ c_0=0x1 ╞═╩═
                                  └─────────┘ 0

        with the timing model:

        .. parsed-literal::
            : Quantum Circuit, first-xgate
            0 ░░▒▒▒░░░░░░░░░░░░░░░
            1 ░░▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒░

            : In wire q0
            Q ░░▒▒▒░░░░░░░░░░░░░░░
            C ░▒░░░░░░░░░░░░░░░░░░ (30dt latency)

            : In wire q1
            Q ░░▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒░
            C ░░▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒░

        See https://arxiv.org/abs/2102.01682 for more details.

    """

    CONDITIONAL_SUPPORTED = (Gate, Delay)

    def __init__(self, durations: InstructionDurations):
        """Scheduler initializer.

        Args:
            durations: Durations of instructions to be used in scheduling
        """
        super().__init__()
        self.durations = durations

        # Ensure op node durations are attached and in consistent unit
        self.requires.append(TimeUnitConversion(durations))

        # Initialize timeslot
        if "node_start_time" in self.property_set:
            warnings.warn(
                "This circuit has been already scheduled. "
                "The output of previous scheduling pass will be overridden.",
                UserWarning,
            )
        self.property_set["node_start_time"] = dict()

    @staticmethod
    def _get_node_duration(
        node: DAGOpNode,
        bit_index_map: Dict,
        dag: DAGCircuit,
    ) -> int:
        """A helper method to get duration from node or calibration."""
        indices = [bit_index_map[qarg] for qarg in node.qargs]

        if dag.has_calibration_for(node):
            # If node has calibration, this value should be the highest priority
            cal_key = tuple(indices), tuple(float(p) for p in node.op.params)
            duration = dag.calibrations[node.op.name][cal_key].duration

            # Note that node duration is updated (but this is analysis pass)
            node.op.duration = duration
        else:
            duration = node.op.duration

        if isinstance(duration, ParameterExpression):
            raise TranspilerError(
                f"Parameterized duration ({duration}) "
                f"of {node.op.name} on qubits {indices} is not bounded."
            )
        if duration is None:
            raise TranspilerError(f"Duration of {node.op.name} on qubits {indices} is not found.")

        return duration

    def run(self, dag: DAGCircuit):
        raise NotImplementedError
