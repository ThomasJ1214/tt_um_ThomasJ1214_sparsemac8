![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg) ![](../../workflows/fpga/badge.svg)

# Sparse 8x8 signed MAC with fused ReLU

An 8-bit by 8-bit signed multiply-accumulate unit that skips zero operands and counts them, with a ReLU and
saturating requantise stage on the output. Built for the Tiny Tapeout TTSKY26c shuttle on the sky130A process.

- [Project datasheet](docs/info.md)

## What it does

At its centre the design runs one operation:

```
acc = acc + (A * B)
```

A and B are 8-bit signed values, holding anything from -128 to +127. The accumulator is 24 bits.

That operation is the multiply-accumulate, usually shortened to MAC. It is the arithmetic underneath matrix
multiplication, which is the arithmetic underneath neural networks. A production accelerator tiles hundreds or
thousands of these into a grid called a systolic array. This project is one unit from such a grid.

Two things are built on top of that core, and both are taken from how real accelerators work rather than
invented for the exercise.

**Sparsity skipping.** After a neural network is pruned, somewhere between half and ninety percent of its
weights are zero. Multiplying by zero cannot change a total, so the multiply is wasted work and wasted power.
This unit detects a zero operand, leaves the accumulator entirely alone so its flip-flops never reload, and
counts the event instead. The count is readable, which turns the chip into an instrument for measuring how
sparse a given workload really is.

**Fused ReLU and requantise.** A real layer does not stop at the accumulator. It clamps negatives to zero,
which is the ReLU activation function, then squeezes the wide total back down to 8 bits for the next layer.
Both are available on the output path, applied in that order.

With the three mode bits low, none of this is active and the chip behaves as a plain MAC unit.

## Block diagram

```
                          ui_in[7:0]   (operand data bus)
                               |
              +----------------+----------------+
              |                                 |
        +-----v------+                    +-----v------+
        |   a_reg    |                    |   b_reg    |
        |  8-bit     |                    |  8-bit     |
        |  signed    |                    |  signed    |
        +-----+------+                    +-----+------+
              |         \                /      |
              |          +--> zero? <---+       |     sparsity detect
              |                 |               |
              +--------+        |      +--------+
                       |        |      |
                 +-----v--------|------v-----+
                 |    signed 8 x 8 multiply  |
                 +-------------+-------------+
                               |  16-bit signed product
                               |  (sign-extended to 24)
                         +-----v-----+        +----------------+
                         |     +     | <------|  acc, 24-bit   |
                         +-----+-----+        |    signed      |
                               |              +--------+-------+
                               +-- skip? -----> hold   |
                                        \              |
                                         +--> +1 --> +-v------------+
                                                     | skip_count   |
                                                     |   16-bit     |
                                                     +------+-------+
                               +----------------------------+
                               |
                    +----------v-----------+
                    |  source select       | <---- uio_in[7]  STAT_SEL
                    +----------+-----------+
                               |
                    +----------v-----------+
                    |  ReLU, max(0, x)     | <---- uio_in[5]  RELU_EN
                    +----------+-----------+
                               |
                    +----------v-----------+
                    |  byte select /       | <---- uio_in[4:3] SEL
                    |  requantise shift    |
                    +----------+-----------+
                               |
                    +----------v-----------+
                    |  saturate or truncate| <---- uio_in[6]  SAT_EN
                    +----------+-----------+
                               |
                          uo_out[7:0]


   uio_in[2:0] ----> opcode decoder ----> write enables for a_reg, b_reg, acc
```

Everything above the output path updates on the rising edge of the clock. Everything from the source select
downwards is combinational, so reading never disturbs what is stored.

## Interface

| Signal        | Direction | Purpose                                          |
|---------------|-----------|--------------------------------------------------|
| `ui_in[7:0]`  | in        | Operand data bus                                 |
| `uio_in[2:0]` | in        | Opcode                                           |
| `uio_in[4:3]` | in        | Result byte select, also the requantise shift    |
| `uio_in[5]`   | in        | RELU_EN, clamp a negative result to zero         |
| `uio_in[6]`   | in        | SAT_EN, saturate the byte instead of truncating  |
| `uio_in[7]`   | in        | STAT_SEL, show the skip counter, not the total   |
| `uo_out[7:0]` | out       | Selected result byte                             |
| `uio_out`     | out       | Tied to `8'h00`                                  |
| `uio_oe`      | out       | Tied to `8'h00`, so every `uio` pin is an input  |
| `rst_n`       | in        | Reset, active low, synchronous                   |

### Opcodes, on `uio_in[2:0]`

| Value | Name   | Effect                                        |
|-------|--------|-----------------------------------------------|
| 000   | NOP    | No state changes                              |
| 001   | LOAD_A | `a_reg <= ui_in`                              |
| 010   | LOAD_B | `b_reg <= ui_in`                              |
| 011   | MAC    | Accumulate, or skip and count if an operand is zero |
| 100   | CLEAR  | `acc <= 0` and `skip_count <= 0`              |
| 101, 110, 111 | (unassigned) | Behave as NOP                    |

### Byte select, on `uio_in[4:3]`

| Value | Shows                                    |
|-------|------------------------------------------|
| 00    | Bits 7 down to 0                         |
| 01    | Bits 15 down to 8                        |
| 10    | Bits 23 down to 16                       |
| 11    | Sign extension: `0x00` positive, `0xFF` negative |

## Why the pins are arranged this way

Tiny Tapeout gives every project the same fixed pinout: 8 dedicated inputs, 8 dedicated outputs, and 8
bidirectional pins. Everything has to fit that budget.

The operands take the whole dedicated input port. An 8-bit signed operand needs all 8 bits to cover -128 to
+127, so there is no room to borrow one for control, and keeping the bus contiguous means the demo board's
bank of switches maps onto one number rather than a number with control bits scattered through it.

Control therefore lives on the bidirectional port. The design never drives anything outwards, so `uio_oe` is
tied to zero and all 8 bidirectional pins act as plain inputs. Three go to the opcode, two to the byte select,
and the last three to the mode bits.

The output is only 8 pins wide and the total is wider, so it is read a byte at a time. That costs two input
bits and no output pins, and it works for a total of any width.

The mode bits were chosen so that all three low reproduces the original plain MAC exactly. That was deliberate:
it means the twelve tests written for the simpler design carry over untouched and still pass, which is a much
stronger check that nothing was broken than writing fresh tests would have been.

One neat consequence of the layout: the byte select doubles as the requantise shift. Reading byte k is the same
as shifting right by 8k and keeping the low eight bits, which is exactly what requantising does. So saturation
needed no extra control pin of its own, just a flag saying whether to clamp or truncate.

## Why the accumulator is 24 bits and not 32

The plain MAC version of this design used 32 bits and came out at 72.9% of a tile. Adding sparsity counting
and the ReLU stage pushed a 32-bit version to roughly 88%, which is uncomfortably close to full.

Dropping the accumulator to 24 bits gave back more than the new features cost. The design now synthesises to
989 cells, slightly fewer than the 1012 of the plain 32-bit version, so both features came in for free in area
terms.

24 bits still holds roughly plus or minus 8.3 million, which is over 500 full-magnitude products before it
wraps, and 24-bit accumulators are a normal choice in real 8-bit accelerators. The width is the parameter
`ACC_W`, so it can be moved again if needed.

## The part worth understanding

The internal product of A and B is 16 bits wide, and that width is deliberate.

Fifteen bits is enough for every product these operands can produce except exactly one. The largest magnitude
comes from squaring the most negative input:

```
-128 * -128 = +16384 = 2^14
```

A 15-bit signed number runs from -16384 to +16383, so +16384 lands directly on the sign bit and reads back as
-16384. The answer is wrong by 32768 and nothing warns you, because as far as the language is concerned nothing
illegal happened. Sixteen bits, which is simply the width of A plus the width of B, has room for it.

The second trap is signedness. Verilog treats a bare vector as unsigned unless told otherwise, and if even one
operand in an expression is unsigned then the whole expression is evaluated as unsigned. Under that rule,
-3 * 4 quietly becomes 253 * 4. The registers here are declared `signed` and the multiply operands are wrapped
in `$signed()` as well, which is redundant but makes the intent impossible to misread.

## Running the tests

The test bench uses cocotb and Icarus Verilog.

```bash
brew install icarus-verilog
python3 -m venv ~/tt-env && source ~/tt-env/bin/activate
pip install -r test/requirements.txt
```

Then:

```bash
cd test
make
```

Twenty-three tests run. The first twelve are inherited unchanged from the plain MAC design and prove the core
arithmetic still works. The rest cover the new features.

| #     | Covers                                                                 |
|-------|------------------------------------------------------------------------|
| 1-6   | Positive, negative and boundary multiplications, including -128 squared |
| 7     | Byte lanes on a large positive total                                    |
| 8     | Reset clears `acc`, `a_reg` and `b_reg`                                 |
| 9     | Unassigned opcodes change nothing                                       |
| 10-11 | CLEAR preserves operands, totals cross zero correctly                   |
| 12    | Byte lanes on a large negative total                                    |
| 13-14 | A zero operand on either side skips the MAC                             |
| 15    | The counter tallies skipped MACs and only skipped ones                  |
| 16    | CLEAR resets the skip counter                                           |
| 17-18 | ReLU clamps negatives and leaves positives alone                        |
| 19-20 | Saturation clamps to +127 and -128                                      |
| 21    | Saturation off still truncates                                          |
| 22    | ReLU is applied before saturation, not after                            |
| 23    | The mode bits never alter stored state                                  |

The test clock is 10 us per cycle, which looks absurdly slow for logic this small. That number is chosen for
the gate-level run rather than for the RTL. After hardening, these same tests are replayed against the
synthesised netlist, which is compiled with every gate given one nanosecond of delay. The multiply and add path
is roughly 25 to 35 gates deep, so a clock in the tens of nanoseconds would sample the result before it had
settled and fail a design that is actually fine. The real speed target lives in `CLOCK_PERIOD` in
[src/config.json](src/config.json).

Every test drives and reads only the real chip pins. None of them reach inside the module to inspect a
register, because the same tests are re-run against the gate-level netlist after the design is hardened, and by
that point the internal register names no longer exist.

Test 8 has to prove that `a_reg` and `b_reg` were cleared even though neither has an output pin of its own. It
does this through the datapath: after a reset it loads a known non-zero value into one register, runs a MAC, and
checks the answer is still zero. That can only be true if the other register really did reset to zero.

## What is Tiny Tapeout?

Tiny Tapeout is an educational project that aims to make it easier and cheaper than ever to get your digital and analog designs manufactured on a real chip.

To learn more and get started, visit https://tinytapeout.com.

## Resources

- [FAQ](https://tinytapeout.com/faq/)
- [Digital design lessons](https://tinytapeout.com/digital_design/)
- [Learn how semiconductors work](https://tinytapeout.com/siliwiz/)
- [Join the community](https://tinytapeout.com/discord)
- [Build your design locally](https://www.tinytapeout.com/guides/local-hardening/)
