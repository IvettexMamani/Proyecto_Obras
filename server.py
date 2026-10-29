"""Servidor MCP de inversiones y obras públicas del Perú.

Fuentes públicas, sin CAPTCHA:
- Invierte.pe, Consulta Avanzada (MEF):
  https://ofi5.mef.gob.pe/invierte/consultaPublica/consultaAvanzada
- Invierte.pe, ficha de seguimiento de inversiones, SSI (MEF):
  https://ofi5.mef.gob.pe/ssi/Ssi/Index?codigo=<CUI>&tipo=2
- INFOBRAS (Contraloría General de la República):
  https://infobras.contraloria.gob.pe/InfobrasWeb/Mapa/Index
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import ssl
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

import httpx
import truststore
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

logging.getLogger("httpx").setLevel(logging.WARNING)

PERU = timezone(timedelta(hours=-5))
SESSION_TTL = 15 * 60  # segundos antes de renovar la sesión con cada sitio
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) invierte-obras-mcp/0.1",
    "X-Requested-With": "XMLHttpRequest",
    "Accept-Language": "es-PE,es;q=0.9",
}
JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}

INVIERTE = "https://ofi5.mef.gob.pe/invierte"
INVIERTE_WS = "https://ofi5.mef.gob.pe/invierteWS"
SSI_FICHA = "https://ofi5.mef.gob.pe/ssi/Ssi/Index"
INFOBRAS = "https://infobras.contraloria.gob.pe/infobrasweb"

ESTADOS_OBRA = {
    "paralizada": "Paralizada",
    "en_ejecucion": "En Ejecución",
    "finalizada": "Finalizado",
    "sin_ejecucion": "Sin Ejecución",
}
SITUACIONES = {
    "en_formulacion": "F",
    "viable": "V",
    "no_viable": "NV",
    "aprobado": "A",
    "no_aprobado": "NA",
}
CAMPOS_OBRA = {
    "codigo_infobras": "codigoObra",
    "cui": "codUniqInv",
    "codigo_snip": "codSnip",
    "nombre": "nombrObra",
    "entidad": "nombreEntidad",
    "provincia": "nombrProvincia",
    "distrito": "nombrDistrito",
    "estado": "estObra",
    "modalidad": "nombrModalidad",
    "tipo_obra": "nombrNivel",
    "monto_obra_soles": "montoObraSoles",
    "fecha_inicio": "fechaIniObra",
    "fecha_fin_prevista": "fechaFinObra",
    "plazo_dias": "plazoObra",
    "avance_fisico_real": "realAvanceFisico",
    "monto_devengado": "montoDevengado",
}
EstadoObra = Literal["paralizada", "en_ejecucion", "finalizada", "sin_ejecucion"]
Situacion = Literal["en_formulacion", "viable", "no_viable", "aprobado", "no_aprobado"]

mcp = MCPServer(
    "invierte-obras",
    instructions=(
        "Datos en vivo de inversiones públicas (Invierte.pe, MEF) y obras públicas "
        "(INFOBRAS, Contraloría) del Perú. Indica siempre la fuente y la fecha de consulta. "
        "Para totales, montos y rankings de obras usa resumen_obras_departamento: no sumes a mano. "
        "El estado de una obra en INFOBRAS lo registra cada entidad y puede estar desactualizado: "
        "para confirmar si una obra sigue paralizada, cruza su CUI con detalle_inversion "
        "(estado en Invierte.pe, último mes con devengado y registro de la Ley 31589). "
        "Los conteos de INFOBRAS no son los mismos que los de los informes trimestrales de "
        "obras paralizadas de la Contraloría, que usan otra metodología."
    ),
)


class Sitio:
    """Cliente HTTP con cookies; abre sesión visitando una página del sitio."""

    def __init__(self, pagina_inicial: str | None = None):
        self.pagina_inicial = pagina_inicial
        self._client: httpx.AsyncClient | None = None
        self._abierta = 0.0
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def client(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if loop is not self._loop:
            # cada asyncio.run (por ejemplo, cada prueba) trae otro event loop: el cliente anterior ya no sirve
            self._client, self._lock, self._loop = None, asyncio.Lock(), loop
        async with self._lock:
            if self._client is None or time.monotonic() - self._abierta > SESSION_TTL:
                if self._client is not None:
                    await self._client.aclose()
                # truststore usa el almacén de certificados del sistema operativo
                ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                self._client = httpx.AsyncClient(
                    verify=ctx, headers=HEADERS, timeout=90, follow_redirects=True
                )
                if self.pagina_inicial:
                    (await self._client.get(self.pagina_inicial)).raise_for_status()
                self._abierta = time.monotonic()
            return self._client


invierte = Sitio(INVIERTE + "/consultaPublica/consultaAvanzada")
ssi = Sitio()
infobras = Sitio(INFOBRAS + "/Mapa/Index")

_departamentos: dict[str, dict[str, str]] = {}
_FECHA_JSON = re.compile(r"^/Date\((-?\d+)\)/$")


def _normalizar(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", sin_tildes).strip().upper()


def _valor(valor: Any) -> Any:
    """Convierte las fechas '/Date(ms)/' de ASP.NET a 'AAAA-MM-DD'."""
    if not isinstance(valor, str):
        return valor
    m = _FECHA_JSON.match(valor)
    if not m:
        return valor.strip()
    fecha = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(m.group(1)))
    return fecha.astimezone(PERU).date().isoformat() if fecha.year > 1900 else None


def _campos(origen: dict[str, Any], mapa: dict[str, str]) -> dict[str, Any]:
    """Renombra los campos de `origen` según `mapa` y omite los vacíos."""
    salida = {}
    for nuevo, original in mapa.items():
        valor = _valor(origen.get(original))
        if valor not in (None, ""):
            salida[nuevo] = valor
    return salida


def _meta(fuente: str) -> dict[str, str]:
    return {
        "fuente": fuente,
        "consultado": datetime.now(PERU).isoformat(timespec="minutes"),
    }


async def _pedir(sitio: Sitio, metodo: str, url: str, fuente: str, **kwargs: Any) -> Any:
    """Hace la petición y devuelve el JSON; los fallos de red llegan al modelo como ToolError."""
    try:
        c = await sitio.client()
        r = await c.request(metodo, url, **kwargs)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ToolError(f"No se pudo consultar {fuente}: {exc!r}") from exc


async def _codigo_departamento(nombre: str, fuente: Literal["invierte", "infobras"]) -> str:
    if fuente not in _departamentos:
        if fuente == "invierte":
            lista = await _pedir(
                invierte, "POST", INVIERTE + "/general/traeListaDepartamento", "Invierte.pe", json={}
            )
            _departamentos[fuente] = {_normalizar(d["Descripcion"]): str(d["Codigo"]) for d in lista}
        else:
            data = await _pedir(
                infobras, "GET", INFOBRAS + "/Mapa/ListaParametrosComentario", "INFOBRAS"
            )
            _departamentos[fuente] = {
                _normalizar(d["Text"]): d["Value"] for d in data["Departamentos"]
            }

    deps = _departamentos[fuente]
    clave = _normalizar(nombre)
    if clave in deps:
        return deps[clave]
    parecidos = [k for k in deps if clave in k]
    if len(parecidos) == 1:
        return deps[parecidos[0]]
    raise ToolError(
        f"Departamento no reconocido: {nombre!r}. Opciones: {', '.join(sorted(deps))}."
    )


async def _pagina_obras(
    codigo_departamento: str, estado: EstadoObra, pagina: int, por_pagina: int
) -> tuple[int, list[dict[str, Any]]]:
    """Una página del buscador de obras de INFOBRAS: el total de obras y las de esa página."""
    filtros = {"estObra": ESTADOS_OBRA[estado], "codDepartamento": codigo_departamento}
    data = await _pedir(
        infobras,
        "POST",
        INFOBRAS + "/Mapa/busqueda/obras",
        "INFOBRAS",
        params={"page": pagina - 1, "rowsPerPage": por_pagina, "Parameters": json.dumps(filtros)},
        headers=JSON_HEADERS,
    )
    if data.get("Code") != 0:
        raise ToolError(f"INFOBRAS respondió con error: {data.get('ErrorDescription')}")
    return int(data["Parameters"]["records"]), [_campos(o, CAMPOS_OBRA) for o in data["Result"] or []]


@mcp.tool()
async def contar_obras_por_departamento(estado: EstadoObra = "paralizada") -> dict[str, Any]:
    """Cuenta las obras públicas de INFOBRAS (Contraloría) en cada departamento según su estado.

    Útil para preguntas como "¿cuántas obras paralizadas hay en Tacna?" o para comparar
    departamentos. El estado lo registra cada entidad en INFOBRAS.
    """
    data = await _pedir(
        infobras,
        "GET",
        INFOBRAS + "/Mapa/busqueda/obras/groupby/departamento",
        "INFOBRAS",
        params={"Parameters": json.dumps({"estObra": ESTADOS_OBRA[estado]})},
    )
    if data.get("Code") != 0:
        raise ToolError(f"INFOBRAS respondió con error: {data.get('ErrorDescription')}")
    conteo = dict(sorted(data["Result"].items(), key=lambda kv: -kv[1]))
    return {
        **_meta("INFOBRAS - Contraloría General de la República"),
        "estado": ESTADOS_OBRA[estado],
        "total": sum(conteo.values()),
        "por_departamento": conteo,
    }


@mcp.tool()
async def listar_obras_infobras(
    departamento: Annotated[str, Field(description="Nombre del departamento, por ejemplo 'Tacna'.")],
    estado: EstadoObra = "paralizada",
    pagina: Annotated[int, Field(ge=1)] = 1,
    por_pagina: Annotated[int, Field(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    """Lista las obras públicas de INFOBRAS de un departamento según su estado.

    Cada obra incluye su CUI (código único de inversión), que se puede pasar a
    detalle_inversion para ver su presupuesto, devengado y registro de paralización en Invierte.pe.
    """
    codigo = await _codigo_departamento(departamento, "infobras")
    total, obras = await _pagina_obras(codigo, estado, pagina, por_pagina)
    return {
        **_meta("INFOBRAS - Contraloría General de la República"),
        "departamento": departamento.upper(),
        "estado": ESTADOS_OBRA[estado],
        "total": total,
        "pagina": pagina,
        "total_paginas": math.ceil(total / por_pagina),
        "monto_total_pagina_soles": round(sum(o.get("monto_obra_soles", 0) for o in obras), 2),
        "obras": obras,
    }


@mcp.tool()
async def resumen_obras_departamento(
    departamento: Annotated[str, Field(description="Nombre del departamento, por ejemplo 'Tacna'.")],
    estado: EstadoObra = "paralizada",
) -> dict[str, Any]:
    """Resume TODAS las obras de INFOBRAS de un departamento según su estado, con las cuentas hechas en Python.

    Úsala para totales y rankings, por ejemplo "¿cuánto suman las obras paralizadas de Puno?" o
    "¿qué entidad tiene más obras paralizadas en Cusco?", en vez de sumar a mano las páginas de
    listar_obras_infobras. Ejemplo de argumentos: departamento="Tacna", estado="paralizada".
    Devuelve el número de obras, el monto total y promedio, la obra de mayor monto y el reparto
    por provincia, entidad (las 10 con más obras), modalidad y año de inicio.
    """
    codigo = await _codigo_departamento(departamento, "infobras")
    total, obras = await _pagina_obras(codigo, estado, 1, 100)
    for pagina in range(2, math.ceil(total / 100) + 1):
        obras += (await _pagina_obras(codigo, estado, pagina, 100))[1]

    def agrupar(campo: str, primeros: int | None = None) -> dict[str, dict[str, Any]]:
        grupos: dict[str, dict[str, Any]] = {}
        for o in obras:
            g = grupos.setdefault(o.get(campo, "SIN DATO"), {"obras": 0, "monto_soles": 0.0})
            g["obras"] += 1
            g["monto_soles"] += o.get("monto_obra_soles", 0)
        orden = sorted(grupos.items(), key=lambda kv: (-kv[1]["obras"], -kv[1]["monto_soles"]))
        return {k: {**v, "monto_soles": round(v["monto_soles"], 2)} for k, v in orden[:primeros]}

    monto_total = sum(o.get("monto_obra_soles", 0) for o in obras)
    mayor = max(obras, key=lambda o: o.get("monto_obra_soles", 0), default={})
    anios = Counter(o["fecha_inicio"][:4] for o in obras if o.get("fecha_inicio"))
    return {
        **_meta("INFOBRAS - Contraloría General de la República"),
        "departamento": departamento.upper(),
        "estado": ESTADOS_OBRA[estado],
        "total_obras": len(obras),
        "monto_total_soles": round(monto_total, 2),
        "monto_promedio_soles": round(monto_total / len(obras), 2) if obras else 0,
        "obras_sin_cui": sum(1 for o in obras if not o.get("cui")),
        "obra_de_mayor_monto": {
            k: mayor[k] for k in ("cui", "nombre", "entidad", "provincia", "monto_obra_soles") if k in mayor
        },
        "por_provincia": agrupar("provincia"),
        "entidades_con_mas_obras": agrupar("entidad", primeros=10),
        "por_modalidad": agrupar("modalidad"),
        "por_anio_inicio": dict(sorted(anios.items())),
    }


@mcp.tool()
async def buscar_inversiones(
    texto: Annotated[
        str | None, Field(description="Palabras que contiene el nombre, por ejemplo 'hospital'.")
    ] = None,
    departamento: Annotated[str | None, Field(description="Por ejemplo 'Tacna'.")] = None,
    situacion: Situacion | None = None,
    costo_minimo: Annotated[float | None, Field(ge=0, description="En soles.")] = None,
    costo_maximo: Annotated[float | None, Field(ge=0, description="En soles.")] = None,
    pagina: Annotated[int, Field(ge=1)] = 1,
    por_pagina: Annotated[int, Field(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """Busca inversiones públicas en el Banco de Inversiones de Invierte.pe (MEF).

    Devuelve costo, situación, entidad y la ejecución presupuestal del año vigente
    (PIA, PIM, devengado) y el devengado acumulado. Requiere al menos un filtro.
    """
    if not any([texto, departamento, situacion, costo_minimo, costo_maximo]):
        raise ToolError("Indica al menos un filtro: texto, departamento, situación o costo.")

    filtros: dict[str, Any] = {
        "filters": "", "ip": "", "cboNom": "1", "txtNom": "",
        "cboDpto": "0", "cboProv": "0", "cboDist": "0",
        "optUf": "*", "cboGNSect": "*", "cboGNPlie": "", "cboGNUF": "",
        "cboGR": "*", "cboGRUf": "", "optGL": "",
        "cboGLDpto": "*", "cboGLProv": "*", "cboGLDist": "*", "cboGLUf": "",
        "cboGLManPlie": "*", "cboGLManUf": "",
        "cboSitu": "*", "cboNivReqViab": "*", "cboEstu": "*", "cboEsta": "*",
        "optFecha": "*", "txtIni": "", "txtFin": "",
        "chkMonto": False, "txtMin": "", "txtMax": "",
        "tipo": "1", "cboFunc": "", "chkInactivo": "0",
        "cboDivision": "", "cboGrupo": "", "rbtnCadena": "",
        "isSearch": False, "PageSize": por_pagina, "PageIndex": pagina,
        "sortField": "", "sortOrder": "asc",
    }  # fmt: skip
    if texto:
        filtros["txtNom"] = texto.strip().upper()  # la búsqueda distingue mayúsculas
    if departamento:
        filtros["cboDpto"] = await _codigo_departamento(departamento, "invierte")
    if situacion:
        filtros["cboSitu"] = SITUACIONES[situacion]
    if costo_minimo is not None or costo_maximo is not None:
        filtros["chkMonto"] = True
        filtros["txtMin"] = str(int(costo_minimo or 0))
        filtros["txtMax"] = str(int(costo_maximo if costo_maximo is not None else 1e13))

    data = await _pedir(
        invierte,
        "POST",
        INVIERTE + "/ConsultaPublica/traeListaProyectoConsultaAvanzada",
        "Invierte.pe",
        json=filtros,
    )
    inversiones = [
        _campos(
            i,
            {
                "cui": "CodigoUnico",
                "nombre": "Nombre",
                "situacion": "Situacion",
                "estado": "Estado",
                "tipo": "TipoFormato",
                "marco": "Marco",
                "nivel_gobierno": "Nivel",
                "sector": "Sector",
                "entidad": "Pliego",
                "unidad_ejecutora": "Ejecutora",
                "departamento": "LocalizacionDepartamento",
                "funcion": "Funcion",
                "monto_viable": "MontoAlternativa",
                # costo total (a+b+c+d): el mismo costo_total_actualizado de detalle_inversion
                # (su costo_actualizado es solo (a))
                "costo_total_actualizado": "Costo",
                "pia_anio_vigente": "PiaActual",
                "pim_anio_vigente": "PimActual",
                "devengado_anio_vigente": "DevActual",
                "devengado_acumulado": "DevAcumulado",
                "beneficiarios": "Beneficiarios",
                "fecha_registro": "FechaRegistro",
                "fecha_viabilidad": "FechaViabilidad",
            },
        )
        for i in data.get("Data") or []
    ]
    return {
        **_meta("Invierte.pe - Banco de Inversiones (MEF)"),
        "total": data.get("TotalRows", 0),
        "pagina": pagina,
        "total_paginas": data.get("TotalPage", 0),
        "inversiones": inversiones,
    }


@mcp.tool()
async def detalle_inversion(
    cui: Annotated[str, Field(description="Código único de inversión (CUI), por ejemplo '2341243'.")],
) -> dict[str, Any]:
    """Ficha de seguimiento de una inversión (SSI de Invierte.pe) a partir de su CUI.

    Incluye costos (el actualizado (a) y el total (a+b+c+d), que suma control concurrente, controversias
    y carta fianza), devengado acumulado y del año, fechas de ejecución, primer y último mes
    con devengado y el registro de obras paralizadas de la Ley 31589. Un CUI puede agrupar
    varias obras, así que un devengado reciente no prueba que una obra concreta esté activa.
    """
    cui = cui.strip()
    if not re.fullmatch(r"\d{4,9}", cui):
        raise ToolError("El CUI debe ser numérico, por ejemplo '2341243'.")

    lista = await _pedir(
        ssi, "POST", INVIERTE_WS + "/Ssi/traeDetInvSSI", "Invierte.pe (SSI)",
        data={"id": cui, "tipo": "2"},
    )
    if not lista:
        raise ToolError(f"No se encontró la inversión con CUI {cui}.")
    d = lista[0]

    detalle = _campos(
        d,
        {
            "cui": "CODIGO_UNICO",
            "codigo_snip": "COD_SNIP",
            "nombre": "NOMBRE_INVERSION",
            "tipo": "TIPO_FORMATO",
            "marco": "MARCO",
            "estado": "ESTADO",
            "situacion": "SITUACION",
            "nivel_gobierno": "NIVEL",
            "sector": "SECTOR",
            "entidad": "ENTIDAD",
            "unidad_ejecutora_inversiones": "DES_UNIDAD_UEI",
            "unidad_formuladora": "DES_UNIDAD_UF",
            "funcion": "FUNCION",
            "programa": "DES_PROGRAMA",
            "subprograma": "DES_SUB_PROGRAMA",
            "beneficiarios": "BENEFICIARIO",
            "modalidad_ejecucion": "MODAL_EJEC",
            "monto_viable": "MTO_VIABLE",
            "costo_actualizado": "COSTO_ACTUALIZADO",
            "costo_control_concurrente": "MTO_CCONCURR_EJE",
            "costo_controversias": "MTO_LAUDO",
            "monto_carta_fianza": "MTO_CartFza",
            "pim_anio_vigente": "PIM_ANO_VIGENTE",
            "devengado_anio_vigente": "DEV_ANO_VIGENTE",
            "devengado_acumulado": "DEV_ACUMULADO",
            "fecha_registro": "FEC_REGISTRO",
            "fecha_viabilidad": "FEC_VIABLE",
            "fecha_inicio_ejecucion": "FEC_INI_EJEC",
            "fecha_fin_ejecucion": "FEC_FIN_EJEC",
            "ultima_actualizacion_formato_12b": "FECHA_ULT_ACT_F12B",
            "avance_fisico_pct": "PORC_AVANCE_FIS",
            "avance_ejecucion_pct": "PORC_AVANCE_EJEC",
            "ultimo_estado_situacional": "ULT_ESTADO_SITUACIONAL",
            "ultimo_problema": "ULT_PROBLEMA",
            "tiene_expediente_tecnico": "ET_REGISTRADO",
            "cierre_registrado": "CIERRE_REGISTRADO",
            "aviso": "DES_MENSAJE",
        },
    )
    detalle["cui"] = cui  # la ficha lo trae como número; buscar_inversiones, como texto
    # como en la ficha web: costo total (a+b+c+d) = actualizado + control concurrente + controversias + carta fianza
    detalle["costo_total_actualizado"] = round(
        sum(d.get(k) or 0 for k in ("COSTO_ACTUALIZADO", "MTO_CCONCURR_EJE", "MTO_LAUDO", "MTO_CartFza")), 2
    )
    costo = d.get("COSTO_ACTUALIZADO") or 0
    if costo:
        acumulado = d.get("DEV_ACUMULADO") or 0
        detalle["saldo_por_ejecutar"] = round(costo - acumulado, 2)
        detalle["avance_financiero_pct"] = round(100 * acumulado / costo, 1)

    finanzas = await _pedir(
        ssi, "POST", INVIERTE_WS + "/Dashboard/traeDevengSSI", "Invierte.pe (SSI)",
        data={"id": cui, "tipo": "FINAN"},
    )
    if finanzas:
        for clave, campo in (("primer_devengado", "PER_PRIM_DEVENG"), ("ultimo_devengado", "PER_ULT_DEVENG")):
            periodo = str(finanzas[0].get(campo) or "")  # formato AAAAMM
            if len(periodo) == 6:
                detalle[clave] = f"{periodo[:4]}-{periodo[4:]}"

    paralizaciones = await _pedir(
        ssi, "POST", INVIERTE + "/paraliza/traeListaParalizaPublico", "Invierte.pe (SSI)",
        data={"id": cui},
    )
    detalle["registro_obras_paralizadas_ley_31589"] = [
        {k.lower(): _valor(v) for k, v in p.items() if v not in (None, "")} for p in paralizaciones
    ]
    detalle["ficha_url"] = f"{SSI_FICHA}?codigo={cui}&tipo=2"
    return {**_meta("Invierte.pe - Seguimiento de Inversiones, SSI (MEF)"), **detalle}


@mcp.resource(
    "invierte://inversion/{cui}",
    name="ficha_inversion",
    title="Ficha de una inversión",
    description="Ficha de seguimiento (SSI de Invierte.pe) de una inversión a partir de su CUI.",
    mime_type="application/json",
)
async def ficha_inversion(cui: str) -> str:
    return json.dumps(await detalle_inversion(cui), ensure_ascii=False, indent=2)


@mcp.resource(
    "infobras://paralizadas/por-departamento",
    name="paralizadas_por_departamento",
    title="Obras paralizadas por departamento",
    description="Conteo de obras paralizadas en INFOBRAS por departamento, consultado al momento.",
    mime_type="application/json",
)
async def paralizadas_por_departamento() -> str:
    return json.dumps(await contar_obras_por_departamento("paralizada"), ensure_ascii=False, indent=2)


@mcp.prompt(title="Informe de obras paralizadas")
def informe_obras(departamento: str) -> str:
    """Informe de las obras paralizadas de un departamento, con cada cifra sacada de INFOBRAS e Invierte.pe."""
    return (
        f"Prepara un informe breve sobre las obras públicas paralizadas en {departamento}.\n\n"
        f"1. Usa resumen_obras_departamento(departamento='{departamento}') para el número de obras, el monto "
        "total y el reparto por provincia y entidad. No sumes a mano.\n"
        "2. Usa contar_obras_por_departamento para ubicar al departamento en el ranking nacional.\n"
        "3. Para las 3 obras de mayor monto que tengan CUI, llama a detalle_inversion y di si siguen "
        "paralizadas según Invierte.pe: estado, último mes con devengado y registro de la Ley 31589.\n"
        "4. Cierra con las advertencias: el estado en INFOBRAS lo registra cada entidad y puede estar "
        "desactualizado, y estos conteos no son los de los informes trimestrales de la Contraloría.\n\n"
        "Formato: un título, tres cifras clave, una tabla con las obras revisadas (CUI, entidad, monto, "
        "estado en Invierte.pe) y la fuente y fecha de consulta de cada cifra."
    )


if __name__ == "__main__":
    mcp.run()
