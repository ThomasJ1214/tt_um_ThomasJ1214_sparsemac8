/*
 * Sparse 8x8 signed MAC unit with a fused ReLU and requantise output stage
 *
 * Copyright (c) 2026 Thomas Jenkins
 * SPDX-License-Identifier: Apache-2.0
 *
 * One processing element of a systolic array. On each clock it can load an
 * operand, or fold a new product into a running total:
 *
 *     acc <= acc + (A * B)
 *
 * A and B are signed 8-bit two's-complement values. The chip has only eight
 * output pins and the accumulator is wider than that, so the result is read
 * back one byte at a time through a byte-select mux.
 *
 * Two features sit on top of that core, both taken from how real neural
 * network accelerators are built:
 *
 *   Sparsity skipping. After a neural network is pruned, most of its weights
 *   are zero, and multiplying by zero is wasted work. When either operand is
 *   zero the accumulator is left alone entirely, so its flip-flops do not
 *   reload, and the event is counted instead. Reading that count back is how
 *   you measure how sparse your data actually is.
 *
 *   Fused ReLU and requantise. A real layer does not stop at the accumulator.
 *   It clamps negative results to zero, which is the ReLU activation function,
 *   and squeezes the wide total back down to 8 bits so it can feed the next
 *   layer. Both steps are available on the output path.
 *
 * All of the added behaviour is off when uio_in[7:5] is 000, in which case the
 * chip behaves exactly like the plain MAC it grew out of.
 */

`default_nettype none

module tt_um_ThomasJ1214_sparsemac8 #(
    // Width of the accumulator. 24 bits holds roughly plus or minus 8.3
    // million, which is over 500 full-magnitude products before it wraps, and
    // 24-bit accumulators are a normal choice for real 8-bit accelerators.
    // It is a parameter so it can be moved if area gets tight. Nothing below
    // hard-codes the width, so changing this one number is the whole change.
    parameter integer ACC_W = 24,

    // Width of the skipped-MAC counter. Must be 32 or less, since it is read
    // back through the same 32-bit output view as the accumulator.
    parameter integer STAT_W = 16
) (
    input  wire [7:0] ui_in,    // Dedicated inputs:  operand data bus
    output wire [7:0] uo_out,   // Dedicated outputs: selected result byte
    input  wire [7:0] uio_in,   // IOs: Input path  = opcode, select, modes
    output wire [7:0] uio_out,  // IOs: Output path = unused, tied low
    output wire [7:0] uio_oe,   // IOs: Enable path = all inputs, so tied low
    input  wire       ena,      // always 1 when the design is powered
    input  wire       clk,      // clock
    input  wire       rst_n     // reset, active LOW
);

  //--------------------------------------------------------------------------
  // Operand and product widths
  //--------------------------------------------------------------------------
  localparam integer A_W = 8;
  localparam integer B_W = 8;

  // A signed A_W x B_W product needs exactly A_W + B_W bits: no more, and
  // critically no fewer. The boundary case is the most negative input squared:
  //
  //     -128 * -128 = +16384 = 2**14
  //
  // Every other 8x8 product fits in 15 bits, so it is tempting to save a bit.
  // Do that and +16384 lands on the sign bit of a 15-bit word and reads back
  // as -16384, silently, with no warning from any tool. Hence the deliberate 16.
  localparam integer PROD_W = A_W + B_W;  // = 16

  //--------------------------------------------------------------------------
  // Control inputs
  //--------------------------------------------------------------------------
  localparam [2:0] OP_NOP    = 3'b000,
                   OP_LOAD_A = 3'b001,
                   OP_LOAD_B = 3'b010,
                   OP_MAC    = 3'b011,
                   OP_CLEAR  = 3'b100;
  // 101, 110 and 111 are unassigned and fall through to the default (no-op).

  wire [2:0] opcode   = uio_in[2:0];
  wire [1:0] byte_sel = uio_in[4:3];
  wire       relu_en  = uio_in[5];  // clamp a negative result to zero
  wire       sat_en   = uio_in[6];  // saturate rather than truncate
  wire       stat_sel = uio_in[7];  // show the skip counter, not the total

  //--------------------------------------------------------------------------
  // State
  //--------------------------------------------------------------------------
  reg signed [   A_W-1:0] a_reg;
  reg signed [   B_W-1:0] b_reg;
  reg signed [ ACC_W-1:0] acc;
  reg        [STAT_W-1:0] skip_count;

  // The $signed() casts are the whole ballgame. Verilog treats a bare vector as
  // UNSIGNED by default, and if even one operand in an expression is unsigned
  // then the entire expression is evaluated unsigned. Drop these casts and
  // -3 * 4 quietly becomes 253 * 4 instead of -12. The registers above are
  // already declared signed, so this is belt and braces, but it is the most
  // common way to get a silently wrong answer in signed Verilog and so it is
  // spelled out.
  wire signed [PROD_W-1:0] product = $signed(a_reg) * $signed(b_reg);

  // Widen the product to the accumulator width before adding, so the sign
  // extension is a visible step rather than something implied by the language.
  // Assigning a signed value to a wider signed wire sign-extends it, which is
  // what is wanted here.
  //
  // The linter flags this deliberate widening, so it is silenced for the one
  // line. The usual alternative, writing the extension out as
  // {{(ACC_W-PROD_W){product[PROD_W-1]}}, product}, is avoided because it
  // becomes a zero-width replication, and therefore illegal, if ACC_W is ever
  // reduced to 16.
  /* verilator lint_off WIDTHEXPAND */
  wire signed [ACC_W-1:0] product_ext = product;
  /* verilator lint_on WIDTHEXPAND */

  // Sparsity detection. Anything times zero is zero, so a MAC with a zero
  // operand cannot change the total. Spotting that lets the accumulator
  // flip-flops stay still instead of reloading the value they already hold,
  // and not toggling flip-flops is where a real sparse accelerator saves power.
  wire operand_is_zero = (a_reg == {A_W{1'b0}}) || (b_reg == {B_W{1'b0}});

  always @(posedge clk) begin
    if (!rst_n) begin
      // Synchronous reset: smaller in area than an asynchronous one, and there
      // is no requirement here to clear state while the clock is stopped.
      a_reg      <= {A_W{1'b0}};
      b_reg      <= {B_W{1'b0}};
      acc        <= {ACC_W{1'b0}};
      skip_count <= {STAT_W{1'b0}};
    end else begin
      case (opcode)
        OP_LOAD_A: a_reg <= $signed(ui_in);
        OP_LOAD_B: b_reg <= $signed(ui_in);
        OP_MAC: begin
          if (operand_is_zero) begin
            // Skip the work and count it instead. The counter wraps when it
            // fills, the same way the accumulator does.
            skip_count <= skip_count + 1'b1;
          end else begin
            // Overflow wraps around. No saturation is applied to the running
            // total itself, only optionally on the way out.
            acc <= acc + product_ext;
          end
        end
        // CLEAR begins a fresh accumulation, so the statistics gathered about
        // the previous one restart alongside it.
        OP_CLEAR: begin
          acc        <= {ACC_W{1'b0}};
          skip_count <= {STAT_W{1'b0}};
        end
        // An empty statement. Inside a clocked block a register that is not
        // assigned simply keeps its value, which is what a no-op means.
        OP_NOP:    ;
        // 101, 110 and 111 are unassigned and behave the same as NOP. A case
        // without a default can infer a latch, so this arm is never omitted.
        default:   ;
      endcase
    end
  end

  //--------------------------------------------------------------------------
  // Output path: choose a source, apply ReLU, pick a byte, optionally saturate
  //--------------------------------------------------------------------------
  // A fixed 32-bit view of each readable value, so the four byte lanes always
  // mean the same thing no matter how wide the registers behind them are. acc
  // is signed and sign-extends, which is why the top lane reads 0x00 for a
  // positive total and 0xFF for a negative one. The counter is a plain count,
  // so it zero-extends instead.
  /* verilator lint_off WIDTHEXPAND */
  wire signed [31:0] acc_view  = acc;
  wire        [31:0] stat_view = skip_count;
  /* verilator lint_on WIDTHEXPAND */

  wire signed [31:0] sel_view = stat_sel ? $signed(stat_view) : acc_view;

  // ReLU, the activation function that sits between neural network layers.
  // It is just max(0, x), so a negative value is replaced by zero.
  wire signed [31:0] activated = (relu_en && sel_view[31]) ? 32'sd0 : sel_view;

  // The byte select doubles as the requantise shift. Reading byte k is the
  // same as shifting right by 8*k and keeping the low eight bits, which is
  // exactly what requantising a wide total down to 8 bits does.
  reg [7:0] raw_byte;
  always @(*) begin
    case (byte_sel)
      2'b00:   raw_byte = activated[7:0];
      2'b01:   raw_byte = activated[15:8];
      2'b10:   raw_byte = activated[23:16];
      2'b11:   raw_byte = activated[31:24];
      // All four values of a 2-bit select are already covered, so this can
      // never be reached in hardware. It is here because a case without a
      // default infers a latch if the tool cannot prove completeness.
      default: raw_byte = activated[7:0];
    endcase
  end

  // Does the value survive that shift without losing information? It does when
  // every bit above the chosen byte is just a copy of that byte's sign bit. If
  // not, plain truncation would wrap a big number into a small wrong one, and
  // clamping to the nearest limit is the more useful answer.
  reg fits;
  always @(*) begin
    case (byte_sel)
      2'b00:   fits = (&activated[31:7])  | (~|activated[31:7]);
      2'b01:   fits = (&activated[31:15]) | (~|activated[31:15]);
      2'b10:   fits = (&activated[31:23]) | (~|activated[31:23]);
      // Nothing sits above the top byte, so it always fits by construction.
      2'b11:   fits = 1'b1;
      default: fits = 1'b1;
    endcase
  end

  // 8'h7F is +127 and 8'h80 is -128, the two ends of the signed 8-bit range.
  wire [7:0] sat_byte = fits ? raw_byte : (activated[31] ? 8'h80 : 8'h7F);

  assign uo_out = sat_en ? sat_byte : raw_byte;

  // Every bidirectional pin is used as an input, so nothing is driven outwards
  // and the output enables stay low.
  assign uio_out = 8'h00;
  assign uio_oe  = 8'h00;

  // ena is always 1 on a powered design, so it is deliberately unused.
  wire _unused = &{ena, 1'b0};

endmodule
