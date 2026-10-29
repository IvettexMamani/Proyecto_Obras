"""El contrato del servidor: si estas pruebas pasan, Claude Code y evaluar.py pueden usarlo.

    uv run pytest -q

Llaman al servidor en memoria, igual que lo haría Claude Code, y piden datos reales a INFOBRAS e Invierte.pe.
Los conteos de INFOBRAS cambian de un día a otro, así que se comparan entre herramientas y no contra un número fijo.
"""
import asyncio
import json

import pytest
from mcp import Client

from server import mcp

CUI_HOSPITAL_TACNA = "2341243"  # Hospital III Daniel Alcides Carrión, EsSalud Tacna


async def _con_cliente(f):
    async with Client(mcp) as c:
        return await f(c)


def llamar(herramienta, **args):
    return asyncio.run(_con_cliente(lambda c: c.call_tool(herramienta, args)))


def datos(herramienta, **args):
    r = llamar(herramienta, **args)
    assert not r.is_error, r.content[0].text
    return r.structured_content


def test_contar_obras_paralizadas():
    r = datos("contar_obras_por_departamento", estado="paralizada")
    assert r["total"] == sum(r["por_departamento"].values()) > 0
    assert "TACNA" in r["por_departamento"]
    assert r["fuente"].startswith("INFOBRAS") and r["consultado"]


def test_listar_obras_de_un_departamento():
    conteo = datos("contar_obras_por_departamento", estado="paralizada")["por_departamento"]["TACNA"]
    r = datos("listar_obras_infobras", departamento="tacna", estado="paralizada", por_pagina=5)
    assert r["total"] == conteo
    assert len(r["obras"]) == min(5, conteo)
    assert all({"nombre", "entidad", "estado"} <= o.keys() for o in r["obras"])


def test_buscar_inversiones():
    r = datos("buscar_inversiones", texto="hospital", departamento="Tacna", por_pagina=100)
    assert CUI_HOSPITAL_TACNA in [i["cui"] for i in r["inversiones"]]
    assert all("HOSPITAL" in i["nombre"] for i in r["inversiones"])


def test_detalle_inversion():
    r = datos("detalle_inversion", cui=CUI_HOSPITAL_TACNA)
    assert r["cui"] == CUI_HOSPITAL_TACNA
    assert "DANIEL ALCIDES CARRI" in r["nombre"]
    assert r["monto_viable"] == pytest.approx(445_775_154.32, abs=0.01)
    assert r["fecha_viabilidad"] == "2021-12-22"
    assert r["costo_total_actualizado"] >= r["costo_actualizado"]
    assert r["ficha_url"].endswith(f"codigo={CUI_HOSPITAL_TACNA}&tipo=2")


def test_errores_claros():
    r = llamar("detalle_inversion", cui="abc")
    assert r.is_error and "CUI" in r.content[0].text
    r = llamar("listar_obras_infobras", departamento="Narnia")
    assert r.is_error and "Narnia" in r.content[0].text


def test_resumen_sin_cuentas_a_mano():
    """La herramienta propia suma todas las páginas de INFOBRAS para que el modelo no lo haga."""
    lista = datos("listar_obras_infobras", departamento="Tacna", estado="paralizada", por_pagina=100)
    r = datos("resumen_obras_departamento", departamento="Tacna", estado="paralizada")
    assert r["total_obras"] == lista["total"]
    assert r["monto_total_soles"] == pytest.approx(sum(o.get("monto_obra_soles", 0) for o in lista["obras"]), abs=1)
    assert r["obra_de_mayor_monto"]["monto_obra_soles"] == max(o.get("monto_obra_soles", 0) for o in lista["obras"])
    assert sum(p["obras"] for p in r["por_provincia"].values()) == r["total_obras"]
    assert r["fuente"].startswith("INFOBRAS")


def test_un_recurso():
    async def leer(c):
        plantillas = (await c.list_resource_templates()).resource_templates
        ficha = await c.read_resource(f"invierte://inversion/{CUI_HOSPITAL_TACNA}")
        return plantillas, ficha.contents[0].text
    plantillas, texto = asyncio.run(_con_cliente(leer))
    assert "invierte://inversion/{cui}" in [p.uri_template for p in plantillas]
    assert "DANIEL ALCIDES CARRI" in json.loads(texto)["nombre"]


def test_un_prompt():
    async def pedir(c):
        nombres = [p.name for p in (await c.list_prompts()).prompts]
        prompt = await c.get_prompt("informe_obras", {"departamento": "Tacna"})
        return nombres, prompt.messages[0].content.text
    nombres, texto = asyncio.run(_con_cliente(pedir))
    assert "informe_obras" in nombres
    assert "Tacna" in texto and "resumen_obras_departamento" in texto
