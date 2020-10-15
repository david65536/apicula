import tiled_fuzzer
import codegen
import pindef
import bslib
import chipdb
import fuse_h4x
import gowin_unpack
from wirenames import wirenames
from multiprocessing.dummy import Pool
from PIL import Image
import numpy as np
import pickle
import json
import re

def dff(mod, cst, row, col, clk=None):
    "make a dff with optional clock"
    name = tiled_fuzzer.make_name("DFF", "DFF")
    dff = codegen.Primitive("DFF", name)
    dff.portmap['CLK'] = clk if clk else name+"_CLK"
    dff.portmap['D'] = name+"_D"
    dff.portmap['Q'] = name+"_Q"
    mod.wires.update(dff.portmap.values())
    mod.primitives[name] = dff
    cst.cells[name] = f"R{row}C{col}"
    return dff.portmap['CLK']

def ibuf(mod, cst, loc, clk=None):
    "make an ibuf with optional clock"
    name = tiled_fuzzer.make_name("IOB", "IBUF")
    iob = codegen.Primitive("IBUF", name)
    iob.portmap["I"] = name+"_I"
    iob.portmap["O"] = clk if clk else name+"_O"

    mod.wires.update([iob.portmap["O"]])
    mod.inputs.update([iob.portmap["I"]])
    mod.primitives[name] = iob
    cst.ports[name] = loc
    return iob.portmap["O"]

with open(f"{tiled_fuzzer.gowinhome}/IDE/share/device/{tiled_fuzzer.device}/{tiled_fuzzer.device}.fse", 'rb') as f:
    fse = fuse_h4x.readFse(f)

with open(f"{tiled_fuzzer.device}.json") as f:
    dat = json.load(f)

with open(f"{tiled_fuzzer.device}_stage1.pickle", 'rb') as f:
    db = pickle.load(f)


clock_pins = pindef.get_clock_locs(
        tiled_fuzzer.device,
        tiled_fuzzer.params['package'],
        header=tiled_fuzzer.params['header'])
# pins appear to be differential with T/C denoting true/complementary
true_pins = [p[0] for p in clock_pins if "GCLKT" in p[1]]

pool = Pool()

def quadrants():
    mod = codegen.Module()
    cst = codegen.Constraints()
    ibuf(mod, cst, true_pins[2], clk="myclk")
    base_bs, _, _, _, _ = tiled_fuzzer.run_pnr(mod, cst, {})

    modules = []
    constrs = []
    idxes = []
    for i in range(2, db.cols):
        for j in [2, db.rows-3]: # avoid bram
            if "DFF0" not in db.grid[j-1][i-1].bels:
                print(i, j)
                continue
            mod = codegen.Module()
            cst = codegen.Constraints()

            ibuf(mod, cst, true_pins[0], clk="myclk")
            dff(mod, cst, j, i, clk="myclk")

            modules.append(mod)
            constrs.append(cst)
            idxes.append((j, i))

    for i in [2, db.cols-2]:
        for j in range(2, db.rows):
            if "DFF0" not in db.grid[j-1][i-1].bels:
                print(i, j)
                continue
            mod = codegen.Module()
            cst = codegen.Constraints()

            ibuf(mod, cst, true_pins[0], clk="myclk")
            dff(mod, cst, j, i, clk="myclk")

            modules.append(mod)
            constrs.append(cst)
            idxes.append((j, i))

    pnr_res = pool.map(lambda param: tiled_fuzzer.run_pnr(*param, {}), zip(modules, constrs))

    res = {}
    for (row, col), (mybs, *_) in zip(idxes, pnr_res):
        sweep_tiles = fuse_h4x.tile_bitmap(fse, mybs^base_bs)

        # find which tap was used
        taps = [r for (r, c, typ), t in sweep_tiles.items() if typ in {13, 14, 15, 16}]

        # find which center tile was used
        t8x = [(r, c) for (r, c, typ), t in sweep_tiles.items() if typ >= 80 and typ < 90]
        rows, cols, _ = res.setdefault(t8x[0], (set(), set(), taps[0]))
        rows.add(row-1)
        cols.add(col-1)

    return res

def center_muxes(ct, rows, cols):
    "Find which mux drives which spine, and maps their inputs to clock pins"

    fr = min(rows)
    dff_locs = [(fr+1, c+1) for c in cols][:len(true_pins)]

    mod = codegen.Module()
    cst = codegen.Constraints()

    ibufs = [ibuf(mod, cst, p) for p in true_pins]
    dffs = [dff(mod, cst, row, col) for row, col in dff_locs]

    bs, _, _, _, _ = tiled_fuzzer.run_pnr(mod, cst, {})

    gb_sources = {}
    gb_destinations = {}

    modules = []
    constrs = []
    for i, pin in enumerate(true_pins):
        mod = codegen.Module()
        cst = codegen.Constraints()
        ibufs = [ibuf(mod, cst, p) for p in true_pins]
        dffs = [dff(mod, cst, row, col) for row, col in dff_locs]
        mod.assigns = list(zip(dffs, ibufs))[:i+1]

        modules.append(mod)
        constrs.append(cst)

    pnr_res = pool.map(lambda param: tiled_fuzzer.run_pnr(*param, {}), zip(modules, constrs))

    base = bs
    for i, (bs_sweep, *_) in enumerate(pnr_res):
        pin = true_pins[i]
        new = base ^ bs_sweep
        base = bs_sweep
        tiles = chipdb.tile_bitmap(db, new)

        try:
            _, _, clk_pips = gowin_unpack.parse_tile_(db, ct[0], ct[1], tiles[ct], noalias=True)
            dest = list(clk_pips.keys())[0]
            src = list(clk_pips.values())[0]
        except (KeyError, IndexError):
            # it seems this uses a dynamically configurable mux routed to VCC/VSS
            continue
        print(i, pin, src, dest)
        gb_destinations[(ct[1], i)] = dest
        gb_sources[src] = pin

    return gb_sources, gb_destinations

def taps():
    "Find which colunm is driven by which tap"
    mod = codegen.Module()
    cst = codegen.Constraints()

    clks = [ibuf(mod, cst, p) for p in true_pins]

    offset = 2
    for i in range(len(true_pins)):
        for j in range(2, db.cols):
            while "DFF0" not in db.grid[i-1+offset][j-1].bels:
                offset += 1
            flop = dff(mod, cst, i+offset, j)

    bs_base, _, _, _, _ = tiled_fuzzer.run_pnr(mod, cst, {})

    modules = []
    constrs = []
    for pin_nr in range(len(true_pins)):
        for col in range(2, db.cols):
            mod = codegen.Module()
            cst = codegen.Constraints()

            clks = [ibuf(mod, cst, p) for p in true_pins]
            offset = 2
            for i, clk in enumerate(clks):
                for j in range(2, db.cols):
                    while "DFF0" not in db.grid[i-1+offset][j-1].bels:
                        offset += 1
                    flop = dff(mod, cst, i+offset, j)
                    if i < pin_nr:
                        mod.assigns.append((flop, clk))
                    elif i == pin_nr and j == col:
                        mod.assigns.append((flop, clk))

            modules.append(mod)
            constrs.append(cst)

    pnr_res = pool.imap(lambda param: tiled_fuzzer.run_pnr(*param, {}), zip(modules, constrs))

    offset = 0
    clks = {}
    complete_taps = set()
    for idx, (sweep_bs, *_) in enumerate(pnr_res):
        sweep_tiles = chipdb.tile_bitmap(db, sweep_bs^bs_base)

        dffs = set()
        tap = None
        gclk = idx//(db.cols-2)
        if idx and idx%(db.cols-2)==0:
            complete_taps.update(clks[gclk-1].keys())
        if gclk == 4: # secondary taps
            complete_taps = set()
        while "DFF0" not in db.grid[gclk+1+offset][2].bels:
            offset += 1
        # print("#"*80)
        for loc, tile in sweep_tiles.items():
            row, col = loc
            _, pips, clk_pips = gowin_unpack.parse_tile_(db, row, col, tile, noalias=True)
            # print(row, idx//(db.cols-2), clk_pips)
            #if row <= gclk: continue
            if row > gclk+offset and (pips['CLK0'].startswith("GB") or pips['CLK1'].startswith("GB")):
                # print("branch", col)
                dffs.add(col)
            if ("GT00" in clk_pips and gclk < 4) or \
            ("GT10" in clk_pips and gclk >= 4):
                # print("tap", col)
                if col not in complete_taps:
                    tap = col
        clks.setdefault(gclk, {}).setdefault(tap, set()).update(dffs)
        #print(complete_taps, clks)
        #if not tap: break

    return clks

pin_re = re.compile(r"IO([TBRL])(\d+)([A-Z])")
banks = {'T': [(0, n) for n in range(db.cols)],
         'B': [(db.rows-1, n) for n in range(db.cols)],
         'L': [(n, 0) for n in range(db.rows)],
         'R': [(n, db.cols-1) for n in range(db.rows)]}
def pin2loc(name):
    side, num, pin = pin_re.match(name).groups()
    return banks[side][int(num)-1], "IOB"+pin

def pin_aliases(quads, srcs):
    aliases = {}
    for ct in quads.keys():
        for mux, pin in srcs.items():
            (row, col), bel = pin2loc(pin)
            iob = db.grid[row][col].bels[bel]
            iob_out = iob.portmap['O']
            src_name = f"R{row+1}C{col+1}_{iob_out}"
            dest_name = f"R{ct[0]+1}C{ct[1]+1}_{mux}"
            aliases[dest_name] = src_name
    return aliases

def spine_aliases(quads, dests, clks):
    aliases = {}
    for ct, (_, cols, spine_row) in quads.items():
        for clk, taps in clks.items():
            quad_cols = cols.intersection(taps.keys())
            for tap in quad_cols:
                try:
                    dest = dests[ct[1], clk]
                except KeyError:
                    continue
                dest_name = f"R{spine_row+1}C{tap+1}_{dest}"
                src_name = f"R{ct[0]+1}C{ct[1]+1}_{dest}"
                aliases[dest_name] = src_name
    return aliases

def tap_aliases(quads):
    aliases = {}
    for _, (rows, cols, spine_row) in quads.items():
        for col in cols:
            for row in rows:
                for src in ["GT00", "GT10"]:
                    src_name = f"R{spine_row+1}C{col+1}_{src}"
                    dest_name = f"R{row+1}C{col+1}_{src}"
                    aliases[dest_name] = src_name

    return aliases

def branch_aliases(quads, clks):
    aliases = {}
    rows = [row for rows, _, _ in quads.values() for row in rows]
    for clk, taps in clks.items():
        if clk < 4:
            src = "GBO0"
        else:
            src = "GBO1"
        for tap, branch_cols in taps.items():
            for row in rows:
                for col in branch_cols:
                    src_name = f"R{row+1}C{tap+1}_{src}"
                    dest_name = f"R{row+1}C{col+1}_GB{clk}0"
                    aliases[dest_name] = src_name

    return aliases

if __name__ == "__main__":
    quads = quadrants()

    srcs = {}
    dests = {}
    for ct, (rows, cols, _) in quads.items():
        # I reverse the pins here because
        # the 8th mux is not fuzzed presently
        true_pins.reverse()
        qsrcs, qdests = center_muxes(ct, rows, cols)
        srcs.update(qsrcs)
        dests.update(qdests)

    clks = taps()

    for _, cols, _ in quads.values():
        # col 0 contains a tap, but not a dff
        if 1 in cols:
            cols.add(0)
            
    print("    quads =", quads)
    print("    srcs =", srcs)
    print("    dests =", dests)
    print("    clks =", clks)

    pa = pin_aliases(quads, srcs)
    sa = spine_aliases(quads, dests, clks)
    ta = tap_aliases(quads)
    ba = branch_aliases(quads, clks)

    db.aliases.update(pa)
    db.aliases.update(sa)
    db.aliases.update(ta)
    db.aliases.update(ba)

    with open(f"{tiled_fuzzer.device}_stage2.pickle", 'wb') as f:
        pickle.dump(db, f)
