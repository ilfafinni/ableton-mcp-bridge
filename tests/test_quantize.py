#!/usr/bin/env python3
"""Prueba la lógica de cuantización del audio_server (sin dependencias ML).

Nota: las duraciones en la salida son PULSOS (beats), no segundos.
A 120 BPM un beat = 0.5 s; una nota de 0.5 s = 1 pulso de duración.

Uso:  python tests/test_quantize.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "python"))

from audio_server import quantize, RHYTHM_PITCH  # noqa: E402


def main():
    failures = []

    def check(label, cond, extra=""):
        print(f"  [{'OK ' if cond else 'FAIL'}] {label} {extra}")
        if not cond:
            failures.append(label)

    # 1 beat exacto al beat 0 a 120 BPM (0.5 s = 1 pulso)
    out = quantize([(60, 0.0, 0.5, 100)], 120.0)
    check("1 beat en el beat 0", len(out) == 1 and out[0]["start_time"] == 0.0
          and out[0]["duration"] == 1.0 and out[0]["pitch"] == 60, str(out))

    # nota fuera de la rejilla -> corchea más cercana (start 0.86 -> 0.75)
    out = quantize([(62, 0.43, 0.9, 90)], 120.0)
    check("cuantiza start a la rejilla", out[0]["start_time"] == 0.75, str(out))

    # sin BPM -> fallback beat 0.5 s: 0.25 s = 0.5 pulsos -> rejilla 0.5
    out = quantize([(64, 0.0, 0.25, 80)], None)
    check("sin bpm usa fallback", out[0]["duration"] == 0.5, str(out))

    # nota negativa recortada a 0
    out = quantize([(65, -0.2, 0.3, 70)], 120.0)
    check("nota negativa -> start 0", out[0]["start_time"] == 0.0, str(out))

    # velocity acotada 1-127
    out = quantize([(66, 0.0, 0.25, 500)], 120.0)
    check("velocity acotada arriba", out[0]["velocity"] == 127, str(out[0]["velocity"]))
    out = quantize([(66, 0.0, 0.25, 0)], 120.0)
    check("velocity acotada abajo", out[0]["velocity"] == 1, str(out[0]["velocity"]))

    # ritmo: nota de kick en el pitch correcto
    out = quantize([(RHYTHM_PITCH, 0.0, 0.25, 90)], 120.0)
    check("ritmo usa pitch de kick", out[0]["pitch"] == RHYTHM_PITCH, str(out))

    # dos notas en el mismo slot -> la segunda se mantiene (sin duplicar)
    out = quantize([(60, 0.0, 0.5, 100), (61, 0.24, 0.5, 100)], 120.0)
    check("2 notas distintas", len(out) == 2, str(out))

    print()
    if failures:
        print(f"FALLARON: {failures}")
        return 1
    print("TODAS LAS PRUEBAS DE CUANTIZACIÓN PASARON")
    return 0


if __name__ == "__main__":
    sys.exit(main())
