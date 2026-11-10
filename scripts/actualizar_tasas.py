"""Descarga tasas de EE.UU. desde FRED (Reserva Federal de St. Louis) y escribe data/tasas.json."""

from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

RAIZ = Path(__file__).resolve().parent.parent
SALIDA = RAIZ / "data" / "tasas.json"
ANIOS_MEDIA = 10
DESDE = (pd.Timestamp.today() - pd.DateOffset(years=ANIOS_MEDIA + 1)).strftime("%Y-%m-%d")

FED = [
    ("fed_sup", "Tasa Fed: rango objetivo (techo)", "DFEDTARU"),
    ("fed_inf", "Tasa Fed: rango objetivo (piso)", "DFEDTARL"),
    ("effr", "Tasa Fed efectiva (EFFR)", "DFF"),
]
CURVA = [  # (id, plazo, serie FRED, en_tabla)
    ("1m", "1 mes", "DGS1MO", True),
    ("3m", "3 meses", "DGS3MO", True),
    ("6m", "6 meses", "DGS6MO", True),
    ("1a", "1 año", "DGS1", True),
    ("2a", "2 años", "DGS2", True),
    ("3a", "3 años", "DGS3", False),
    ("5a", "5 años", "DGS5", True),
    ("7a", "7 años", "DGS7", False),
    ("10a", "10 años", "DGS10", True),
    ("20a", "20 años", "DGS20", False),
    ("30a", "30 años", "DGS30", True),
]

_sesion = requests.Session()
_sesion.headers["User-Agent"] = "rok.ar (indicadores personales)"


def fred(serie: str) -> pd.Series:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={serie}&cosd={DESDE}"
    r = _sesion.get(url, timeout=60)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    s = pd.to_numeric(df.iloc[:, 1], errors="coerce")
    s.index = pd.to_datetime(df.iloc[:, 0])
    s = s.dropna().sort_index()
    if len(s) < 30:
        raise ValueError(f"{serie}: serie demasiado corta")
    return s


def valor_al(s: pd.Series, fecha: pd.Timestamp) -> float | None:
    previo = s[s.index <= fecha]
    return float(previo.iloc[-1]) if len(previo) else None


def metricas(id_: str, nombre: str, s: pd.Series, fuente: str) -> dict:
    fecha = s.index[-1]
    actual = float(s.iloc[-1])
    base = valor_al(s, pd.Timestamp(fecha.year - 1, 12, 31))
    ventana = s[s.index > fecha - pd.DateOffset(years=ANIOS_MEDIA)]
    media = float(ventana.mean())
    return {
        "id": id_, "nombre": nombre, "fecha": fecha.strftime("%Y-%m-%d"), "valor": actual,
        "ytd_pb": (actual - base) * 100 if base is not None else None,
        "ytd_pct": (actual / base - 1) * 100 if base else None,
        "media": media, "media_desde": ventana.index[0].strftime("%Y-%m-%d"),
        "desvio_pb": (actual - media) * 100,
        "desvio_pct": (actual / media - 1) * 100 if media else None,
        "fuente": fuente, "desactualizado": False,
    }


def main() -> int:
    previo = json.loads(SALIDA.read_text()) if SALIDA.exists() else {}
    prev_filas = {f["id"]: f for grupo in ("fed", "curva") for f in previo.get(grupo, [])}
    series, errores = {}, 0

    def fila(id_, nombre, serie):
        nonlocal errores
        try:
            s = fred(serie)
            series[id_] = s
            print(f"ok    {serie}")
            return metricas(id_, nombre, s, f"FRED ({serie})")
        except Exception as e:
            errores += 1
            print(f"ERROR {serie}: {e}", file=sys.stderr)
            return dict(prev_filas.get(id_, {"id": id_, "nombre": nombre, "valor": None}), desactualizado=True)

    fed = [fila(i, n, s) for i, n, s in FED]
    curva_todos = {i: fila(i, f"Bono del Tesoro {p}", s) for i, p, s, _ in CURVA}
    curva = [curva_todos[i] for i, _, _, en_tabla in CURVA if en_tabla]

    if errores == len(FED) + len(CURVA):
        print("Fallaron todas las series; no se toca el archivo.", file=sys.stderr)
        return 1

    # Pendiente 10a - 2a
    if "10a" in series and "2a" in series:
        df = pd.concat([series["10a"], series["2a"]], axis=1, keys=["l", "c"]).dropna()
        pend = df["l"] - df["c"]
        f = metricas("10a2a", "Pendiente 10 años – 2 años", pend, "FRED (DGS10 − DGS2)")
        f["ytd_pct"] = None
        f["desvio_pct"] = None  # % de un spread que cruza el cero no tiene sentido
        curva.append(f)
    elif "10a2a" in prev_filas:
        curva.append(dict(prev_filas["10a2a"], desactualizado=True))

    # Curvas para el gráfico: hoy, cierre del año anterior y hace un año
    grafico = previo.get("grafico")
    disponibles = [i for i, *_ in CURVA if i in series]
    if len(disponibles) >= 6:
        hoy = max(s.index[-1] for s in series.values())
        fechas = {
            "hoy": hoy,
            "inicio_anio": pd.Timestamp(hoy.year - 1, 12, 31),
            "hace_un_anio": hoy - pd.DateOffset(years=1),
        }
        grafico = {"plazos": [p for _, p, _, _ in CURVA], "curvas": []}
        for clave, f in fechas.items():
            vals = [valor_al(series[i], f) if i in series else None for i, *_ in CURVA]
            ult = max(series[i][series[i].index <= f].index[-1] for i in disponibles)
            grafico["curvas"].append({"id": clave, "fecha": ult.strftime("%Y-%m-%d"), "valores": vals})

    SALIDA.parent.mkdir(exist_ok=True)
    SALIDA.write_text(json.dumps({
        "actualizado": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "anios_media": ANIOS_MEDIA,
        "fed": fed, "curva": curva, "grafico": grafico,
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
