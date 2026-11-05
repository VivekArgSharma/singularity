"""Procedural primitive meshes for Panda3D (no external model files).

Every mesh sits with its base on z=0 so objects rest on the floor.
"""
import math

from panda3d.core import (Geom, GeomNode, GeomTriangles, GeomVertexData,
                          GeomVertexFormat, GeomVertexWriter)


def _build(name, verts, norms, tris, uvs=None):
    fmt = GeomVertexFormat.getV3n3t2() if uvs else GeomVertexFormat.getV3n3()
    vdata = GeomVertexData(name, fmt, Geom.UHStatic)
    vw = GeomVertexWriter(vdata, "vertex")
    nw = GeomVertexWriter(vdata, "normal")
    tw = GeomVertexWriter(vdata, "texcoord") if uvs else None
    for i, (v, n) in enumerate(zip(verts, norms)):
        vw.addData3(*v)
        nw.addData3(*n)
        if tw:
            tw.addData2(*uvs[i])
    prim = GeomTriangles(Geom.UHStatic)
    for a, b, c in tris:
        prim.addVertices(a, b, c)
    geom = Geom(vdata)
    geom.addPrimitive(prim)
    node = GeomNode(name)
    node.addGeom(geom)
    return node


def _quad(verts, norms, tris, corners, normal):
    base = len(verts)
    verts.extend(corners)
    norms.extend([normal] * 4)
    tris.append((base, base + 1, base + 2))
    tris.append((base, base + 2, base + 3))


def box(sx, sy, sz, name="box"):
    hx, hy = sx / 2, sy / 2
    v, n, t = [], [], []
    _quad(v, n, t, [(hx, -hy, 0), (hx, hy, 0), (hx, hy, sz), (hx, -hy, sz)], (1, 0, 0))
    _quad(v, n, t, [(-hx, hy, 0), (-hx, -hy, 0), (-hx, -hy, sz), (-hx, hy, sz)], (-1, 0, 0))
    _quad(v, n, t, [(hx, hy, 0), (-hx, hy, 0), (-hx, hy, sz), (hx, hy, sz)], (0, 1, 0))
    _quad(v, n, t, [(-hx, -hy, 0), (hx, -hy, 0), (hx, -hy, sz), (-hx, -hy, sz)], (0, -1, 0))
    _quad(v, n, t, [(-hx, -hy, sz), (hx, -hy, sz), (hx, hy, sz), (-hx, hy, sz)], (0, 0, 1))
    _quad(v, n, t, [(-hx, hy, 0), (hx, hy, 0), (hx, -hy, 0), (-hx, -hy, 0)], (0, 0, -1))
    return _build(name, v, n, t)


def wedge(sx, sy, sz, name="wedge"):
    """Ramp: low edge at x=-sx/2 (z=0) rising to z=sz at x=+sx/2, full width sy."""
    hx, hy = sx / 2, sy / 2
    v, n, t = [], [], []
    k = math.hypot(sx, sz)
    _quad(v, n, t, [(-hx, -hy, 0), (hx, -hy, sz), (hx, hy, sz), (-hx, hy, 0)], (-sz / k, 0, sx / k))   # slope
    _quad(v, n, t, [(hx, -hy, 0), (hx, hy, 0), (hx, hy, sz), (hx, -hy, sz)], (1, 0, 0))                 # high end
    _quad(v, n, t, [(-hx, hy, 0), (hx, hy, 0), (hx, -hy, 0), (-hx, -hy, 0)], (0, 0, -1))                # bottom
    for side in (-1, 1):                                                                                    # side triangles
        base = len(v)
        y = side * hy
        v.extend([(-hx, y, 0), (hx, y, 0), (hx, y, sz)])
        n.extend([(0, side, 0)] * 3)
        t.append((base, base + 1, base + 2) if side > 0 else (base, base + 2, base + 1))
    return _build(name, v, n, t)


def cylinder(r, h, seg=40, name="cylinder"):
    v, n, t = [], [], []
    for i in range(seg):
        a0, a1 = 2 * math.pi * i / seg, 2 * math.pi * (i + 1) / seg
        c0, s0, c1, s1 = math.cos(a0), math.sin(a0), math.cos(a1), math.sin(a1)
        base = len(v)
        v += [(r * c0, r * s0, 0), (r * c1, r * s1, 0), (r * c1, r * s1, h), (r * c0, r * s0, h)]
        n += [(c0, s0, 0), (c1, s1, 0), (c1, s1, 0), (c0, s0, 0)]
        t += [(base, base + 1, base + 2), (base, base + 2, base + 3)]
        base = len(v)  # top cap
        v += [(0, 0, h), (r * c0, r * s0, h), (r * c1, r * s1, h)]
        n += [(0, 0, 1)] * 3
        t.append((base, base + 1, base + 2))
        base = len(v)  # bottom cap
        v += [(0, 0, 0), (r * c1, r * s1, 0), (r * c0, r * s0, 0)]
        n += [(0, 0, -1)] * 3
        t.append((base, base + 1, base + 2))
    return _build(name, v, n, t)


def cone(r, h, seg=40, name="cone"):
    v, n, t = [], [], []
    k = math.hypot(r, h)
    for i in range(seg):
        a0, a1 = 2 * math.pi * i / seg, 2 * math.pi * (i + 1) / seg
        am = (a0 + a1) / 2
        c0, s0, c1, s1 = math.cos(a0), math.sin(a0), math.cos(a1), math.sin(a1)
        base = len(v)
        v += [(r * c0, r * s0, 0), (r * c1, r * s1, 0), (0, 0, h)]
        n += [(h * c0 / k, h * s0 / k, r / k), (h * c1 / k, h * s1 / k, r / k),
              (h * math.cos(am) / k, h * math.sin(am) / k, r / k)]
        t.append((base, base + 1, base + 2))
        base = len(v)
        v += [(0, 0, 0), (r * c1, r * s1, 0), (r * c0, r * s0, 0)]
        n += [(0, 0, -1)] * 3
        t.append((base, base + 1, base + 2))
    return _build(name, v, n, t)


def sphere(r, rings=18, seg=32, name="sphere"):
    v, n, t = [], [], []
    for i in range(rings + 1):
        phi = math.pi * i / rings
        for j in range(seg + 1):
            th = 2 * math.pi * j / seg
            x, y, z = math.sin(phi) * math.cos(th), math.sin(phi) * math.sin(th), -math.cos(phi)
            v.append((r * x, r * y, r + r * z))
            n.append((x, y, z))
    w = seg + 1
    for i in range(rings):
        for j in range(seg):
            a, b = i * w + j, i * w + j + 1
            c, d = (i + 1) * w + j + 1, (i + 1) * w + j
            t += [(a, b, c), (a, c, d)]
    return _build(name, v, n, t)


def disc(r, seg=40, name="disc"):
    v, n, t = [(0, 0, 0)], [(0, 0, 1)], []
    for i in range(seg + 1):
        a = 2 * math.pi * i / seg
        v.append((r * math.cos(a), r * math.sin(a), 0))
        n.append((0, 0, 1))
    for i in range(1, seg + 1):
        t.append((0, i, i + 1))
    return _build(name, v, n, t)


def flat_rect(x0, y0, x1, y1, uv_scale=None, name="rect"):
    v = [(x0, y0, 0), (x1, y0, 0), (x1, y1, 0), (x0, y1, 0)]
    n = [(0, 0, 1)] * 4
    t = [(0, 1, 2), (0, 2, 3)]
    uvs = None
    if uv_scale:
        uvs = [(x0 / uv_scale, y0 / uv_scale), (x1 / uv_scale, y0 / uv_scale),
               (x1 / uv_scale, y1 / uv_scale), (x0 / uv_scale, y1 / uv_scale)]
    return _build(name, v, n, t, uvs)


def fan(r, a0_deg, a1_deg, seg=24, name="fan"):
    """Flat circular sector on z=0, angles in degrees (0 = +Y, positive towards +X)."""
    v, n, t = [(0, 0, 0)], [(0, 0, 1)], []
    for i in range(seg + 1):
        a = math.radians(a0_deg + (a1_deg - a0_deg) * i / seg)
        v.append((r * math.sin(a), r * math.cos(a), 0))
        n.append((0, 0, 1))
    for i in range(1, seg + 1):
        t.append((0, i + 1, i))
    return _build(name, v, n, t)
