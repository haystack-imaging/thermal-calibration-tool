"""Minimal ctypes wrapper for the DJI Thermal SDK v1.7 (libdirp): R-JPEG -> float32 C.

The SDK's sub-libraries (libv_*.so, libMicro*.so) are dlopen'ed by bare name,
so the SDK directory must be on LD_LIBRARY_PATH *before* Python starts; this
module re-executes the interpreter with it set when needed.
"""
import ctypes as C
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SDK_DIR = os.path.join(HERE, "vendor", "thermal_parser", "plugins", "dji_thermal_sdk_v1.7_20241205", "linux", "release_x64")


def ensure_library_path():
    if SDK_DIR not in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep):
        env = dict(os.environ, LD_LIBRARY_PATH=os.pathsep.join(p for p in (SDK_DIR, os.environ.get("LD_LIBRARY_PATH", "")) if p))
        os.execve(sys.executable, [sys.executable] + sys.argv, env)


class Params(C.Structure):
    # SDK v1.7 added ambient_temp; declaring only four floats lets the SDK write past the struct (heap corruption)
    _fields_ = [("distance", C.c_float), ("humidity", C.c_float), ("emissivity", C.c_float), ("reflection", C.c_float), ("ambient_temp", C.c_float)]


class Resolution(C.Structure):
    _fields_ = [("width", C.c_uint32), ("height", C.c_uint32)]


class DjiThermal:
    def __init__(self, verbose: int = 1):
        self.lib = C.CDLL(os.path.join(SDK_DIR, "libdirp.so"))
        L = self.lib
        L.dirp_set_verbose_level.argtypes = [C.c_int]
        L.dirp_create_from_rjpeg.argtypes = [C.c_char_p, C.c_int32, C.POINTER(C.c_void_p)]
        L.dirp_create_from_rjpeg.restype = C.c_int32
        L.dirp_destroy.argtypes = [C.c_void_p]; L.dirp_destroy.restype = C.c_int32
        L.dirp_get_rjpeg_resolution.argtypes = [C.c_void_p, C.POINTER(Resolution)]; L.dirp_get_rjpeg_resolution.restype = C.c_int32
        L.dirp_get_measurement_params.argtypes = [C.c_void_p, C.POINTER(Params)]; L.dirp_get_measurement_params.restype = C.c_int32
        L.dirp_set_measurement_params.argtypes = [C.c_void_p, C.POINTER(Params)]; L.dirp_set_measurement_params.restype = C.c_int32
        L.dirp_measure_ex.argtypes = [C.c_void_p, C.POINTER(C.c_float), C.c_int32]; L.dirp_measure_ex.restype = C.c_int32
        L.dirp_set_verbose_level(verbose)

    def temperature(self, path, emissivity=None, distance=None, humidity=None, reflection=None):
        """Return (float32 array in C, params actually used). None keeps the value stored in the R-JPEG."""
        import numpy as np
        with open(path, "rb") as f:
            raw = f.read()
        buf = C.create_string_buffer(raw, len(raw))
        h = C.c_void_p()
        rc = self.lib.dirp_create_from_rjpeg(buf, len(raw), C.byref(h))
        if rc != 0:
            raise RuntimeError(f"dirp_create_from_rjpeg failed ({rc}) for {path}")
        try:
            res = Resolution(); self._ok(self.lib.dirp_get_rjpeg_resolution(h, C.byref(res)), "resolution")
            p = Params(); self._ok(self.lib.dirp_get_measurement_params(h, C.byref(p)), "get params")
            for k, v in (("emissivity", emissivity), ("distance", distance), ("humidity", humidity), ("reflection", reflection)):
                if v is not None:
                    setattr(p, k, float(v))
            self._ok(self.lib.dirp_set_measurement_params(h, C.byref(p)), "set params")
            n = int(res.width) * int(res.height)
            out = np.zeros(n, dtype=np.float32)
            self._ok(self.lib.dirp_measure_ex(h, out.ctypes.data_as(C.POINTER(C.c_float)), n * 4), "measure")
            used = {k: round(getattr(p, k), 3) for k, _ in Params._fields_}
            return out.reshape(int(res.height), int(res.width)), used
        finally:
            self.lib.dirp_destroy(h)

    @staticmethod
    def _ok(rc, what):
        if rc != 0:
            raise RuntimeError(f"DJI SDK {what} failed ({rc})")
