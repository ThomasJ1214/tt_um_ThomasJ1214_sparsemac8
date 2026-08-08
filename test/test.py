# SPDX-FileCopyrightText: (c) 2026 Thomas Jenkins
# SPDX-License-Identifier: Apache-2.0
#
# Testbench for the sparse 8x8 signed MAC unit.
#
# Everything here is black-box: the tests only ever drive the real chip pins and
# read the real chip pins. Nothing reaches inside the module to peek at a
# register. That matters because the same tests are re-run against the
# gate-level netlist after hardening, and by then the internal register names
# are gone, so a test that peeked inside would break.

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, Timer

# A deliberately slow clock, 10 us per cycle, which is 100 kHz.
#
# This looks far slower than the hardware needs, and for the RTL simulation it
# is. The reason is the gate-level run. After the design is hardened, these same
# tests are re-run against the synthesised netlist, which the Makefile compiles
# with UNIT_DELAY=#1, giving every single gate one nanosecond of delay. The
# multiply-and-add path is roughly 25 to 35 gates deep, so it needs tens of
# nanoseconds to settle in that mode. A clock in that range would latch garbage
# and the gate-level test would fail even though the silicon is fine. 10 us
# leaves room for any realistic logic depth.
#
# The real timing target is unaffected by this. That is set by CLOCK_PERIOD in
# src/config.json, which is what static timing analysis signs off against.
CLK_PERIOD_US = 10

# After a clock edge the flip-flops update, then the combinational output path
# settles a moment later. Waiting before sampling uo_out avoids reading the
# value from before the edge, and it is generous for the same gate-delay reason.
SETTLE_US = 1

# Accumulator width in the hardware. Values outside the signed range this gives
# would wrap, so the tests below stay inside it on purpose.
ACC_BITS = 24
ACC_MAX = 2 ** (ACC_BITS - 1) - 1
ACC_MIN = -(2 ** (ACC_BITS - 1))

# Opcodes, driven on uio_in[2:0]. These mirror the localparams in the Verilog.
OP_NOP = 0b000
OP_LOAD_A = 0b001
OP_LOAD_B = 0b010
OP_MAC = 0b011
OP_CLEAR = 0b100


def ctrl(opcode, byte_sel=0, relu=0, sat=0, stat=0):
    """Pack the opcode, byte select and mode bits into the uio_in bus layout."""
    return (stat << 7) | (sat << 6) | (relu << 5) | (byte_sel << 3) | opcode


def u8(value):
    """Python int -> 8-bit two's complement, the way the data bus carries it."""
    return value & 0xFF


def u32(value):
    """Python int -> 32-bit two's complement.

    The hardware sign-extends the accumulator to a fixed 32-bit view before the
    byte lanes tap it, so reading all four lanes of a negative total gives the
    32-bit two's complement of that number even though the register is 24 bits.
    """
    return value & 0xFFFFFFFF


async def setup(dut):
    """Start the clock and put the design through a clean reset.

    Every test calls this and gets its own clock. cocotb shuts down any task a
    test started once that test finishes, so a clock launched in test 1 is gone
    by test 2. Each test has to start its own or it will sit waiting on an edge
    that never arrives.
    """
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_US, unit="us").start())

    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = ctrl(OP_NOP)
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    await Timer(SETTLE_US, unit="us")


async def step(dut, opcode, data=0, byte_sel=0, relu=0, sat=0, stat=0):
    """Present one instruction and let the next rising edge execute it."""
    dut.ui_in.value = u8(data)
    dut.uio_in.value = ctrl(opcode, byte_sel, relu, sat, stat)
    await RisingEdge(dut.clk)
    await Timer(SETTLE_US, unit="us")


async def read_byte(dut, byte_sel, relu=0, sat=0, stat=0):
    """Read one byte lane. No clock edge, because the output path is wiring."""
    dut.uio_in.value = ctrl(OP_NOP, byte_sel, relu, sat, stat)
    await Timer(SETTLE_US, unit="us")
    return int(dut.uo_out.value)


async def read_view(dut, relu=0, sat=0, stat=0):
    """Read all four byte lanes of whichever value is currently selected.

    Deliberately no clock edges in here. The output path is combinational, so
    walking the select lines is enough to see all 32 bits. The opcode is held at
    NOP throughout, so the free-running clock cannot disturb the value mid-read.
    """
    value = 0
    for sel in range(4):
        value |= (await read_byte(dut, sel, relu, sat, stat)) << (8 * sel)
    return value


async def read_acc(dut):
    """Read the running total."""
    return await read_view(dut)


async def read_skips(dut):
    """Read the count of MACs that were skipped because an operand was zero."""
    return await read_view(dut, stat=1)


# ---------------------------------------------------------------------------
# Core MAC behaviour. These carry over unchanged from the plain MAC design, so
# they also serve as proof that adding the new features broke nothing.
# ---------------------------------------------------------------------------


@cocotb.test()
async def test_01_positive_multiply(dut):
    """3 * 4 = 12, starting from a cleared accumulator."""
    await setup(dut)
    await step(dut, OP_CLEAR)
    await step(dut, OP_LOAD_A, 3)
    await step(dut, OP_LOAD_B, 4)
    await step(dut, OP_MAC)

    got = await read_acc(dut)
    assert got == 12, f"expected 12, got {got}"


@cocotb.test()
async def test_02_accumulates(dut):
    """A second MAC adds on top of the first: 12 + 30 = 42."""
    await setup(dut)
    await step(dut, OP_CLEAR)
    await step(dut, OP_LOAD_A, 3)
    await step(dut, OP_LOAD_B, 4)
    await step(dut, OP_MAC)
    assert await read_acc(dut) == 12, "first MAC did not land"

    await step(dut, OP_LOAD_A, 5)
    await step(dut, OP_LOAD_B, 6)
    await step(dut, OP_MAC)

    got = await read_acc(dut)
    assert got == 42, f"expected 42, got {got}"


@cocotb.test()
async def test_03_negative_times_positive(dut):
    """-3 * 4 = -12, which reads back as 0xFFFFFFF4."""
    await setup(dut)
    await step(dut, OP_CLEAR)
    await step(dut, OP_LOAD_A, u8(-3))
    await step(dut, OP_LOAD_B, 4)
    await step(dut, OP_MAC)

    got = await read_acc(dut)
    assert got == u32(-12), f"expected 0x{u32(-12):08X}, got 0x{got:08X}"


@cocotb.test()
async def test_04_negative_times_negative(dut):
    """-3 * -4 = +12. Two negatives must come back positive."""
    await setup(dut)
    await step(dut, OP_CLEAR)
    await step(dut, OP_LOAD_A, u8(-3))
    await step(dut, OP_LOAD_B, u8(-4))
    await step(dut, OP_MAC)

    got = await read_acc(dut)
    assert got == 12, f"expected 12, got 0x{got:08X}"


@cocotb.test()
async def test_05_most_negative_squared(dut):
    """-128 * -128 = +16384. This is the one that catches a too-narrow product.

    +16384 is 2**14, so it is the only 8x8 signed product that needs the full
    16th bit. Size the internal product at 15 bits and this silently comes back
    as -16384 instead.
    """
    await setup(dut)
    await step(dut, OP_CLEAR)
    await step(dut, OP_LOAD_A, u8(-128))
    await step(dut, OP_LOAD_B, u8(-128))
    await step(dut, OP_MAC)

    got = await read_acc(dut)
    assert got == 16384, f"expected 16384, got 0x{got:08X}"


@cocotb.test()
async def test_06_most_positive_squared(dut):
    """127 * 127 = 16129, the largest positive product."""
    await setup(dut)
    await step(dut, OP_CLEAR)
    await step(dut, OP_LOAD_A, 127)
    await step(dut, OP_LOAD_B, 127)
    await step(dut, OP_MAC)

    got = await read_acc(dut)
    assert got == 16129, f"expected 16129, got 0x{got:08X}"


@cocotb.test()
async def test_07_byte_select_sweep(dut):
    """Every byte lane reads back the right slice of a large positive total."""
    await setup(dut)
    await step(dut, OP_CLEAR)
    await step(dut, OP_LOAD_A, 127)
    await step(dut, OP_LOAD_B, 127)

    reps = 400  # 400 * 16129 = 6,451,600, comfortably inside a 24-bit signed total
    for _ in range(reps):
        await step(dut, OP_MAC)
    expected = 127 * 127 * reps
    assert expected <= ACC_MAX, "test would overflow the accumulator"

    # No clock edges below: changing the byte select alone has to be enough.
    for sel in range(4):
        got = await read_byte(dut, sel)
        want = (u32(expected) >> (8 * sel)) & 0xFF
        assert got == want, f"byte {sel}: expected 0x{want:02X}, got 0x{got:02X}"


@cocotb.test()
async def test_08_reset_clears_all_state(dut):
    """Reset asserted mid-sequence wipes acc, a_reg and b_reg.

    a_reg and b_reg have no pins of their own, so they are checked through the
    datapath: multiply by a known non-zero operand and confirm the answer is
    still zero, which is only possible if the other operand really is zero.
    """
    await setup(dut)
    await step(dut, OP_CLEAR)
    await step(dut, OP_LOAD_A, 7)
    await step(dut, OP_LOAD_B, 9)
    await step(dut, OP_MAC)
    assert await read_acc(dut) == 63, "setup failed, state was never made dirty"

    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    await Timer(SETTLE_US, unit="us")

    got = await read_acc(dut)
    assert got == 0, f"acc survived reset: 0x{got:08X}"

    # b_reg == 0? Load a known non-zero A and multiply. 100 * b_reg must be 0.
    await step(dut, OP_LOAD_A, 100)
    await step(dut, OP_MAC)
    got = await read_acc(dut)
    assert got == 0, f"b_reg survived reset, 100 * b_reg = 0x{got:08X}"

    # a_reg == 0? Reset again so A is clean, then multiply by a known non-zero B.
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    await Timer(SETTLE_US, unit="us")

    await step(dut, OP_LOAD_B, 100)
    await step(dut, OP_MAC)
    got = await read_acc(dut)
    assert got == 0, f"a_reg survived reset, a_reg * 100 = 0x{got:08X}"


@cocotb.test()
async def test_09_reserved_opcodes_do_nothing(dut):
    """Opcodes 101, 110 and 111 are undefined and must behave like NOP."""
    await setup(dut)
    await step(dut, OP_CLEAR)
    await step(dut, OP_LOAD_A, 5)
    await step(dut, OP_LOAD_B, 6)
    await step(dut, OP_MAC)
    assert await read_acc(dut) == 30, "setup failed"

    # Hold a disruptive value on the data bus the whole time, so an opcode that
    # accidentally decoded as LOAD_A or LOAD_B would corrupt an operand.
    for bad in (0b101, 0b110, 0b111):
        await step(dut, bad, 0x7F)
        got = await read_acc(dut)
        assert got == 30, f"opcode {bad:03b} changed acc to 0x{got:08X}"

    await step(dut, OP_MAC)
    got = await read_acc(dut)
    assert got == 60, f"reserved opcodes corrupted an operand: got {got}, want 60"


@cocotb.test()
async def test_10_clear_preserves_operands(dut):
    """CLEAR zeroes the accumulator and leaves A and B untouched."""
    await setup(dut)
    await step(dut, OP_CLEAR)
    await step(dut, OP_LOAD_A, 11)
    await step(dut, OP_LOAD_B, 7)
    await step(dut, OP_MAC)
    assert await read_acc(dut) == 77, "setup failed"

    await step(dut, OP_CLEAR)
    assert await read_acc(dut) == 0, "CLEAR did not zero the accumulator"

    await step(dut, OP_MAC)
    got = await read_acc(dut)
    assert got == 77, f"CLEAR disturbed an operand: got {got}, want 77"


@cocotb.test()
async def test_11_accumulate_through_zero(dut):
    """A running total can go negative and come back without losing its sign."""
    await setup(dut)
    await step(dut, OP_CLEAR)

    await step(dut, OP_LOAD_A, u8(-100))
    await step(dut, OP_LOAD_B, 1)
    for _ in range(3):
        await step(dut, OP_MAC)
    got = await read_acc(dut)
    assert got == u32(-300), f"expected -300, got 0x{got:08X}"

    await step(dut, OP_LOAD_A, 100)
    for _ in range(4):
        await step(dut, OP_MAC)
    got = await read_acc(dut)
    assert got == 100, f"expected +100 after crossing zero, got 0x{got:08X}"


@cocotb.test()
async def test_12_large_negative_byte_lanes(dut):
    """The byte lanes read back a large negative total correctly.

    Test 7 sweeps the lanes with a positive value. This drives the total well
    negative so the upper lanes carry sign bits rather than zeros.
    """
    await setup(dut)
    await step(dut, OP_CLEAR)
    await step(dut, OP_LOAD_A, u8(-128))
    await step(dut, OP_LOAD_B, 127)

    reps = 400  # 400 * -16256 = -6,502,400, inside a 24-bit signed total
    for _ in range(reps):
        await step(dut, OP_MAC)
    expected = -128 * 127 * reps
    assert expected >= ACC_MIN, "test would overflow the accumulator"

    got = await read_acc(dut)
    assert got == u32(expected), f"expected 0x{u32(expected):08X}, got 0x{got:08X}"


# ---------------------------------------------------------------------------
# Sparsity skipping
# ---------------------------------------------------------------------------


@cocotb.test()
async def test_13_skips_when_a_is_zero(dut):
    """A MAC with A = 0 leaves the total alone and counts itself as skipped."""
    await setup(dut)
    await step(dut, OP_CLEAR)
    await step(dut, OP_LOAD_A, 0)
    await step(dut, OP_LOAD_B, 42)
    await step(dut, OP_MAC)

    assert await read_acc(dut) == 0, "a zero operand should not move the total"
    got = await read_skips(dut)
    assert got == 1, f"expected 1 skipped MAC, got {got}"


@cocotb.test()
async def test_14_skips_when_b_is_zero(dut):
    """A MAC with B = 0 is skipped too, and real MACs still work afterwards."""
    await setup(dut)
    await step(dut, OP_CLEAR)
    await step(dut, OP_LOAD_A, 7)
    await step(dut, OP_LOAD_B, 0)
    await step(dut, OP_MAC)

    assert await read_acc(dut) == 0, "a zero operand should not move the total"
    assert await read_skips(dut) == 1, "skip was not counted"

    # Load a real operand and the unit goes back to doing actual work.
    await step(dut, OP_LOAD_B, 6)
    await step(dut, OP_MAC)
    assert await read_acc(dut) == 42, "MAC did not resume after a skip"
    got = await read_skips(dut)
    assert got == 1, f"a real MAC should not be counted as skipped, got {got}"


@cocotb.test()
async def test_15_counts_many_skips(dut):
    """The counter tallies every skipped MAC, and only the skipped ones."""
    await setup(dut)
    await step(dut, OP_CLEAR)
    await step(dut, OP_LOAD_A, 0)
    await step(dut, OP_LOAD_B, 5)

    skipped = 7
    for _ in range(skipped):
        await step(dut, OP_MAC)
    got = await read_skips(dut)
    assert got == skipped, f"expected {skipped} skips, got {got}"
    assert await read_acc(dut) == 0, "total moved during skipped MACs"

    # Three real MACs on top: total moves, skip count holds still.
    await step(dut, OP_LOAD_A, 3)
    for _ in range(3):
        await step(dut, OP_MAC)
    assert await read_acc(dut) == 45, "real MACs did not accumulate"
    got = await read_skips(dut)
    assert got == skipped, f"skip count changed during real MACs: {got}"


@cocotb.test()
async def test_16_clear_resets_the_skip_counter(dut):
    """CLEAR starts a fresh accumulation, so the statistics restart with it."""
    await setup(dut)
    await step(dut, OP_CLEAR)
    await step(dut, OP_LOAD_A, 0)
    await step(dut, OP_LOAD_B, 9)
    for _ in range(4):
        await step(dut, OP_MAC)
    assert await read_skips(dut) == 4, "setup failed"

    await step(dut, OP_CLEAR)
    got = await read_skips(dut)
    assert got == 0, f"CLEAR left {got} skips on the counter"


# ---------------------------------------------------------------------------
# Fused ReLU and requantise output stage
# ---------------------------------------------------------------------------


@cocotb.test()
async def test_17_relu_clamps_a_negative_total(dut):
    """With ReLU on, a negative total reads as zero on every lane."""
    await setup(dut)
    await step(dut, OP_CLEAR)
    await step(dut, OP_LOAD_A, u8(-3))
    await step(dut, OP_LOAD_B, 4)
    await step(dut, OP_MAC)

    assert await read_acc(dut) == u32(-12), "total should be -12 with ReLU off"
    got = await read_view(dut, relu=1)
    assert got == 0, f"ReLU should have clamped -12 to 0, got 0x{got:08X}"


@cocotb.test()
async def test_18_relu_leaves_a_positive_total_alone(dut):
    """ReLU is max(0, x), so a positive total passes straight through."""
    await setup(dut)
    await step(dut, OP_CLEAR)
    await step(dut, OP_LOAD_A, 6)
    await step(dut, OP_LOAD_B, 7)
    await step(dut, OP_MAC)

    got = await read_view(dut, relu=1)
    assert got == 42, f"ReLU altered a positive total: got {got}"


@cocotb.test()
async def test_19_saturation_clamps_large_positive(dut):
    """A total too big for one byte saturates to +127 instead of wrapping."""
    await setup(dut)
    await step(dut, OP_CLEAR)
    await step(dut, OP_LOAD_A, 127)
    await step(dut, OP_LOAD_B, 127)
    await step(dut, OP_MAC)  # 16129, which is 0x3F01

    # Lane 0 cannot hold 16129, so saturation pins it at +127.
    got = await read_byte(dut, 0, sat=1)
    assert got == 0x7F, f"expected +127 (0x7F), got 0x{got:02X}"

    # Lane 1 is 16129 >> 8 = 63, which fits, so it passes through untouched.
    got = await read_byte(dut, 1, sat=1)
    assert got == 0x3F, f"expected 63 (0x3F), got 0x{got:02X}"


@cocotb.test()
async def test_20_saturation_clamps_large_negative(dut):
    """A total too negative for one byte saturates to -128."""
    await setup(dut)
    await step(dut, OP_CLEAR)
    await step(dut, OP_LOAD_A, u8(-128))
    await step(dut, OP_LOAD_B, 127)
    await step(dut, OP_MAC)  # -16256

    got = await read_byte(dut, 0, sat=1)
    assert got == 0x80, f"expected -128 (0x80), got 0x{got:02X}"

    # -16256 >> 8 is -64, which fits in a signed byte and passes through.
    got = await read_byte(dut, 1, sat=1)
    assert got == u8(-64), f"expected -64 (0xC0), got 0x{got:02X}"


@cocotb.test()
async def test_21_saturation_off_still_truncates(dut):
    """With saturation off the lanes truncate, exactly as they always did."""
    await setup(dut)
    await step(dut, OP_CLEAR)
    await step(dut, OP_LOAD_A, 127)
    await step(dut, OP_LOAD_B, 127)
    await step(dut, OP_MAC)  # 16129 == 0x3F01

    got = await read_byte(dut, 0, sat=0)
    assert got == 0x01, f"expected raw low byte 0x01, got 0x{got:02X}"


@cocotb.test()
async def test_22_relu_and_saturation_together(dut):
    """A big negative total with both features on gives zero, not -128.

    ReLU is applied first, so the value is already zero by the time saturation
    looks at it. Getting -128 here would mean the two stages are in the wrong
    order.
    """
    await setup(dut)
    await step(dut, OP_CLEAR)
    await step(dut, OP_LOAD_A, u8(-128))
    await step(dut, OP_LOAD_B, 127)
    await step(dut, OP_MAC)  # -16256

    got = await read_byte(dut, 0, relu=1, sat=1)
    assert got == 0x00, f"expected 0x00 from ReLU then saturate, got 0x{got:02X}"


@cocotb.test()
async def test_23_mode_bits_never_change_stored_state(dut):
    """The three mode bits only affect the view, never the registers.

    Every instruction below runs with ReLU, saturation and stat-select all
    driven high. If any of them leaked into the state machine the final total
    would be wrong.
    """
    await setup(dut)
    await step(dut, OP_CLEAR, relu=1, sat=1, stat=1)
    await step(dut, OP_LOAD_A, 5, relu=1, sat=1, stat=1)
    await step(dut, OP_LOAD_B, 6, relu=1, sat=1, stat=1)
    await step(dut, OP_MAC, relu=1, sat=1, stat=1)

    got = await read_acc(dut)
    assert got == 30, f"mode bits disturbed the datapath: got {got}, want 30"
    got = await read_skips(dut)
    assert got == 0, f"mode bits caused a phantom skip: {got}"
