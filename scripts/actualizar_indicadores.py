"""Descarga los indicadores de rok.ar y escribe data/indicadores.json.

Corre todos los días hábiles en GitHub Actions (.github/workflows/indicadores.yml).
Si una fuente falla, se conserva el último dato bueno de ese indicador y se lo marca
como desactualizado, en vez de romper toda la tabla.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

RAIZ = Path(__file__).resolve().parent.parent
SALIDA = RAIZ / "data" / "indicadores.json"
ARGDATOS = "https://api.argentinadatos.com"
ANIOS_MEDIA = 10


# ---------- Fuentes ----------

def yahoo(ticker: str) -> pd.Series:
    import yfinance as yf

    hist = yf.Ticker(ticker).history(period=f"{ANIOS_MEDIA + 1}y", interval="1d", auto_adjust=False)
    s = hist["Close"].dropna()
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    return _limpiar(s)


_cache_argdatos: dict[str, list] = {}


def argdatos(ruta: str, campo: str) -> pd.Series:
    if ruta not in _cache_argdatos:
        r = requests.get(ARGDATOS + ruta, timeout=30)
        r.raise_for_status()
        _cache_argdatos[ruta] = r.json()
    df = pd.DataFrame(_cache_argdatos[ruta])
    s = pd.Series(df[campo].astype(float).values, index=pd.to_datetime(df["fecha"]).dt.normalize())
    return _limpiar(s)


def _limpiar(s: pd.Series, positivos: bool = True) -> pd.Series:
    s = s.dropna()
    if positivos:
        s = s[s > 0]
    s = s[~s.index.duplicated(keep="last")].sort_index()
    if len(s) < 30:
        raise ValueError(f"serie demasiado corta ({len(s)} datos)")
    return s.astype(float)


def ccl() -> pd.Series:
    return argdatos("/v1/cotizaciones/dolares/contadoconliqui", "venta")


def merval_usd() -> pd.Series:
    merv = yahoo("^MERV")
    tc = ccl().reindex(merv.index, method="ffill", limit=5)
    return _limpiar((merv / tc).dropna())


def brecha() -> pd.Series:
    oficial = argdatos("/v1/cotizaciones/dolares/oficial", "venta")
    c = ccl()
    df = pd.concat([c, oficial], axis=1, keys=["ccl", "of"]).dropna()
    return _limpiar((df["ccl"] / df["of"] - 1) * 100, positivos=False)


# ---------- Definición de la tabla ----------
# modo "precio": variaciones en % sobre el nivel.
# modo "tasa":   variaciones en diferencia absoluta (pb o pp), porque un % de una tasa confunde.

INDICADORES = [
    dict(id="spx", nombre="S&P 500", unidad="pts", dec=0, modo="precio",
         fuente="Yahoo Finance", serie=lambda: yahoo("^GSPC")),
    dict(id="ndx", nombre="Nasdaq Composite", unidad="pts", dec=0, modo="precio",
         fuente="Yahoo Finance", serie=lambda: yahoo("^IXIC")),
    dict(id="vix", nombre="VIX (volatilidad S&P 500)", unidad="pts", dec=2, modo="precio",
         fuente="Yahoo Finance", serie=lambda: yahoo("^VIX")),
    dict(id="ust10", nombre="Tasa Tesoro EE.UU. 10 años", unidad="%", dec=2, modo="tasa",
         dif_unidad="pb", dif_mult=100, fuente="Yahoo Finance", serie=lambda: yahoo("^TNX")),
    dict(id="dxy", nombre="Índice dólar (DXY)", unidad="pts", dec=2, modo="precio",
         fuente="Yahoo Finance", serie=lambda: yahoo("DX-Y.NYB")),
    dict(id="brent", nombre="Petróleo Brent", unidad="US$/bbl", dec=2, modo="precio",
         fuente="Yahoo Finance", serie=lambda: yahoo("BZ=F")),
    dict(id="oro", nombre="Oro", unidad="US$/oz", dec=0, modo="precio",
         fuente="Yahoo Finance", serie=lambda: yahoo("GC=F")),
    dict(id="mervusd", nombre="S&P Merval en dólares (CCL)", unidad="US$", dec=0, modo="precio",
         fuente="Yahoo Finance / ArgentinaDatos", serie=merval_usd),
    dict(id="riesgo", nombre="Riesgo país (EMBI Argentina)", unidad="pb", dec=0, modo="tasa",
         dif_unidad="pb", dif_mult=1, fuente="ArgentinaDatos", serie=lambda: argdatos("/v1/finanzas/indices/riesgo-pais", "valor")),
    dict(id="brecha", nombre="Brecha CCL vs. oficial", unidad="%", dec=1, modo="tasa",
         dif_unidad="pp", dif_mult=1, desvio_abs=True, fuente="ArgentinaDatos", serie=brecha),
]


# ---------- Cálculo ----------

def calcular(defn: dict, s: pd.Series) -> dict:
    fecha = s.index[-1]
    actual = float(s.iloc[-1])
    previo = float(s.iloc[-2])
    anterior_anio = s[s.index.year < fecha.year]
    base_ytd = float(anterior_anio.iloc[-1]) if len(anterior_anio) else float(s[s.index.year == fecha.year].iloc[0])
    ventana = s[s.index > fecha - pd.DateOffset(years=ANIOS_MEDIA)]
    media = float(ventana.mean())
    desde = ventana.index[0]

    if defn["modo"] == "precio":
        var_dia, var_u = (actual / previo - 1) * 100, "%"
        ytd, ytd_u = (actual / base_ytd - 1) * 100, "%"
    else:
        m = defn["dif_mult"]
        var_dia, var_u = (actual - previo) * m, defn["dif_unidad"]
        ytd, ytd_u = (actual - base_ytd) * m, defn["dif_unidad"]

    if defn.get("desvio_abs"):
        desvio, desvio_u = actual - media, "pp"
    else:
        desvio, desvio_u = (actual / media - 1) * 100, "%"

    return {
        "id": defn["id"], "nombre": defn["nombre"], "unidad": defn["unidad"], "decimales": defn["dec"],
        "fecha": fecha.strftime("%Y-%m-%d"), "valor": actual,
        "var_dia": var_dia, "var_dia_unidad": var_u,
        "ytd": ytd, "ytd_unidad": ytd_u,
        "media": media, "media_desde": desde.strftime("%Y-%m-%d"),
        "desvio": desvio, "desvio_unidad": desvio_u,
        "fuente": defn["fuente"], "desactualizado": False,
    }


def main() -> int:
    previos = {}
    if SALIDA.exists():
        try:
            previos = {i["id"]: i for i in json.loads(SALIDA.read_text())["indicadores"]}
        except Exception:
            previos = {}

    filas, errores = [], 0
    for d in INDICADORES:
        try:
            filas.append(calcular(d, d["serie"]()))
            print(f"ok    {d['id']}")
        except Exception as e:  # una fuente caída no tira abajo las demás
            errores += 1
            print(f"ERROR {d['id']}: {e}", file=sys.stderr)
            if d["id"] in previos:
                fila = dict(previos[d["id"]], desactualizado=True)
            else:
                fila = {"id": d["id"], "nombre": d["nombre"], "fuente": d["fuente"], "valor": None, "desactualizado": True}
            filas.append(fila)

    if errores == len(INDICADORES):
        print("Fallaron todas las fuentes; no se toca el archivo.", file=sys.stderr)
        return 1

    SALIDA.parent.mkdir(exist_ok=True)
    SALIDA.write_text(json.dumps({
        "actualizado": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "anios_media": ANIOS_MEDIA,
        "indicadores": filas,
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
