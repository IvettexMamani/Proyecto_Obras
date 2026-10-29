"""grafico.png: resultado de la evaluación por modo y obras paralizadas por departamento según el MCP.

    uv run grafico.py
"""
import asyncio
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
from mcp import Client

from server import mcp

AQUI = Path(__file__).parent
SUPERFICIE, TINTA, TINTA_2, REJILLA = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
RESULTADOS = {"acierta": ("#0ca30c", "✓ acierta"), "se equivoca": ("#d03b3b", "✗ se equivoca"),
              "no sabe": ("#a3a29d", "? no sabe")}
AZUL, AZUL_OSCURO = "#86b6ef", "#184f95"
MODOS = {"mcp": "MCP\ninvierte-obras", "web": "Búsqueda web\n(WebSearch, WebFetch)", "memoria": "Memoria\n(sin herramientas)"}


async def conteo_paralizadas():
    async with Client(mcp) as c:
        return (await c.call_tool("contar_obras_por_departamento", {"estado": "paralizada"})).structured_content


def ejes_limpios(ax):
    ax.set_facecolor(SUPERFICIE)
    for lado in ("top", "right", "left"):
        ax.spines[lado].set_visible(False)
    ax.spines["bottom"].set_color(REJILLA)
    ax.tick_params(colors=TINTA_2, length=0)
    ax.grid(axis="x", color=REJILLA, linewidth=0.8)
    ax.set_axisbelow(True)


res = pd.read_csv(AQUI / "resultados.csv")
tabla = res.groupby("modo").resultado.value_counts().unstack(fill_value=0).reindex(columns=list(RESULTADOS), fill_value=0)
tabla = tabla.reindex([m for m in MODOS if m in tabla.index])  # barh dibuja de abajo arriba: memoria queda arriba
conteo = asyncio.run(conteo_paralizadas())
deps = pd.Series(conteo["por_departamento"]).sort_values()

fig, (a, b) = plt.subplots(1, 2, figsize=(12, 6.5), gridspec_kw={"width_ratios": [1, 1.25]}, facecolor=SUPERFICIE)

# Panel A: preguntas por resultado y modo
ejes_limpios(a)
izquierda = pd.Series(0, index=tabla.index)
for resultado, (color, etiqueta) in RESULTADOS.items():
    n = tabla[resultado]
    a.barh(tabla.index, n, left=izquierda, height=0.35, color=color, edgecolor=SUPERFICIE, linewidth=2, label=etiqueta)
    for modo in tabla.index:
        if n[modo]:
            a.text(izquierda[modo] + n[modo] / 2, modo, f"{n[modo]} de {len(res) // len(tabla)}", ha="center",
                   va="center", color="white" if resultado != "no sabe" else TINTA, fontsize=12, fontweight="bold")
    izquierda += n
a.set_yticks(range(len(tabla)), [MODOS[m] for m in tabla.index], color=TINTA, fontsize=10)
a.set_xlim(0, len(res) // len(tabla))
a.set_xlabel("preguntas", color=TINTA_2)
a.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=3, frameon=False, labelcolor=TINTA_2)
a.set_title(f"Aciertos por modo: {len(res) // len(tabla)} preguntas, Claude Haiku", loc="left", color=TINTA,
            fontsize=11, fontweight="bold")
segundos = res.groupby("modo").segundos.mean()
a.text(0, -0.30, "Segundos promedio por pregunta: " + " · ".join(f"{m} {segundos[m]:.1f}" for m in reversed(tabla.index)),
       transform=a.transAxes, color=TINTA_2, fontsize=9)

# Panel B: obras paralizadas por departamento, desde la herramienta del MCP
ejes_limpios(b)
colores = [AZUL_OSCURO if d == "TACNA" else AZUL for d in deps.index]
b.barh(deps.index, deps.values, height=0.7, color=colores, edgecolor=SUPERFICIE, linewidth=1)
b.tick_params(axis="y", labelsize=8, labelcolor=TINTA)
for d in list(deps.index[-3:]) + ["TACNA"]:
    b.text(deps[d] + 3, d, f"{deps[d]}", va="center", color=TINTA, fontsize=9,
           fontweight="bold" if d == "TACNA" else "normal")
b.set_xlabel("obras con estado «Paralizada»", color=TINTA_2)
b.set_title(f"Obras paralizadas por departamento ({conteo['total']:,})", loc="left", color=TINTA,
            fontsize=11, fontweight="bold")

fig.text(0.01, 0.01, f"Fuente: INFOBRAS (Contraloría) vía el MCP invierte-obras, consultado {conteo['consultado'][:10]}; "
         "evaluación con evaluar.py (claude -p).", color=TINTA_2, fontsize=8)
fig.tight_layout(rect=(0, 0.03, 1, 1))
fig.savefig(AQUI / "grafico.png", dpi=150, facecolor=SUPERFICIE)
print("grafico.png listo")
