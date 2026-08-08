<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

At its centre this chip does one thing, the operation that sits underneath almost all neural network maths:

```
acc = acc + (A * B)
```

Multiply two numbers, add the answer to a running total. That is a multiply-accumulate, or MAC. A real neural
network accelerator contains hundreds or thousands of these wired into a grid. This project is one of them,
built on its own so you can watch it work.

A and B are 8-bit signed operands, so each holds a whole number from -128 to +127, stored in two's complement.
The accumulator is 24 bits, which reaches roughly plus or minus 8.3 million. That is over 500 full-size
products before it wraps, and 24-bit accumulators are a normal choice for real 8-bit accelerators.

You drive the chip one instruction at a time. The opcode goes on `uio[2:0]`, any data goes on `ui[7:0]`, and
the instruction takes effect on the next rising clock edge.

| Opcode | Name   | What it does                          |
|--------|--------|---------------------------------------|
| 000    | NOP    | Nothing changes                       |
| 001    | LOAD_A | Copy `ui[7:0]` into register A        |
| 010    | LOAD_B | Copy `ui[7:0]` into register B        |
| 011    | MAC    | Add A times B to the running total    |
| 100    | CLEAR  | Reset the total and the skip counter  |
| 101, 110, 111 | (unused) | Treated the same as NOP        |

Pulling `rst_n` low clears everything on the next clock edge.

### Sparsity skipping

Once a neural network has been pruned, most of its weights are zero. Multiplying by zero cannot change a total,
so doing the multiply is wasted work and wasted power.

This chip notices. If either operand is zero when a MAC is issued, the accumulator is left completely alone,
so its flip-flops never reload, and the event is added to a counter instead. That counter is 16 bits and can be
read out, which turns the chip into a way of measuring how sparse your data actually is. Set `uio[7]` high and
the output shows the skip count rather than the total.

### Fused ReLU and requantise

A real neural network layer does not stop at the accumulator. It clamps negative results to zero, which is the
ReLU activation function, and then squeezes the wide total back down to 8 bits so it can feed the next layer.
Both steps are built into the output path here.

Setting `uio[5]` high applies ReLU, so any negative value reads as zero.

Setting `uio[6]` high turns on saturation. Without it, reading a byte simply truncates, so a total of 16129
read through the lowest byte gives 1, which is a meaningless number. With saturation on, a value too large for
the byte is clamped to +127 or -128 instead, which is what a real requantiser does.

ReLU is applied before saturation, matching the order in a real layer.

### Reading the result

The total is wider than the eight output pins, so it comes out one byte at a time. Two bits, `uio[4:3]`,
choose which byte appears on `uo[7:0]`.

| `uio[4:3]` | Byte shown on `uo[7:0]` |
|------------|-------------------------|
| 00         | Bits 7 down to 0        |
| 01         | Bits 15 down to 8       |
| 10         | Bits 23 down to 16      |
| 11         | Sign extension of the total |

The accumulator is 24 bits, so the top selection shows sign extension: `0x00` when the total is positive and
`0xFF` when it is negative. That makes it a quick way to read the sign.

The byte selector is pure combinational logic. Changing the two select bits changes the output straight away,
with no clock edge needed, so reading can never disturb the value being read.

With `uio[7:5]` all low, none of the extra features are active and the chip behaves exactly like a plain MAC
unit.

### The detail most worth knowing

The product of two 8-bit signed numbers is held in 16 bits. Fifteen would be enough for every product except
one. The exception is -128 * -128, which is +16384, exactly 2 to the power 14. Squeeze that into 15 bits and
the value lands on the sign bit and reads back as -16384 instead. No tool warns you. The design uses the full
16 bits for that reason, and a test fails if anyone ever narrows it.

## How to test

The quickest check is the simulation. With Icarus Verilog and cocotb installed:

```
cd test
make
```

That runs 23 tests covering positive and negative operands, accumulation, the byte selector, reset, unused
opcodes, sparsity skipping, the skip counter, ReLU, and saturation. All 23 should pass.

On the demo board, three controls matter. The data bus `ui[7:0]` comes from the bank of input DIP switches.
Everything on `uio[7:0]` is driven by the on-board RP2040, which you set from the Commander app. The clock can
be single stepped from the Commander app's INTERACT tab, which is what makes it possible to walk through a
calculation one instruction at a time. The answer appears on the 7-segment display.

Every instruction follows the same rhythm: set up the inputs, then advance the clock one step.

To work out 3 * 4 = 12:

1. Pull `rst_n` low, then release it, to start from a clean state.
2. Set the opcode to `100` (CLEAR) and pulse the clock.
3. Set `ui[7:0]` to 3 (`0000 0011`), set the opcode to `001` (LOAD_A), and pulse the clock.
4. Set `ui[7:0]` to 4 (`0000 0100`), set the opcode to `010` (LOAD_B), and pulse the clock.
5. Set the opcode to `011` (MAC) and pulse the clock.
6. Set the opcode back to `000` (NOP) with `uio[4:3]` at `00`. The output reads 12 (`0000 1100`).

To see a negative result, repeat with `ui[7:0]` set to `1111 1101` for A, which is -3. The answer is -12, and
the byte lanes read `F4`, `FF`, `FF`, `FF` from lowest to highest.

To watch sparsity skipping, run the same sequence but load 0 into A. Every MAC now leaves the total at zero.
Set `uio[7]` high and the output shows how many MACs were skipped, counting up one per MAC you issue.

To see ReLU, build a negative total, then flip `uio[5]` high. The output drops to zero and stays there while
the bit is high. Flip it back and the negative total reappears, because ReLU only changes the view and never
the stored value.

To see saturation, load 127 into both A and B and run one MAC, giving 16129. With `uio[6]` low the lowest byte
reads `01`, which is the truncated remainder and not a useful answer. With `uio[6]` high it reads `7F`, which
is +127, correctly reporting that the total is larger than a byte can hold.

## External hardware

None. Everything the design needs is already on the demo board: the input DIP switches for the data bus, the
RP2040 for the control pins and for stepping the clock, and the 7-segment display for the result.
