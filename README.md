# MCP de inversiones y obras públicas del Perú

Servidor MCP, `invierte-obras`, que le da a Claude Code datos en vivo de **Invierte.pe** (MEF) y de **INFOBRAS**
(Contraloría): obras paralizadas por departamento, montos, fichas de seguimiento de un CUI y el registro de obras
paralizadas de la Ley 31589. Los portales son públicos y el servidor no necesita claves.

Es el proyecto de la Sesión 04 del curso (*MCP en Claude Code*), hecho sobre un tema propio en lugar de la plantilla
del BCRP. En la evaluación, con 10 preguntas y Claude Haiku, la memoria acertó 0, la búsqueda web 0 y el MCP 10.
Los detalles están en [`informe.md`](informe.md), y el proceso completo en [`PROCESO.md`](PROCESO.md) y
[`informe_mcp.ipynb`](informe_mcp.ipynb).

| Pieza | Nombre |
|---|---|
| Herramientas | `contar_obras_por_departamento`, `listar_obras_infobras`, `buscar_inversiones`, `detalle_inversion` |
| Herramienta propia | `resumen_obras_departamento`: recorre todas las páginas de INFOBRAS y devuelve el número de obras, el monto total y promedio, la obra de mayor monto y el reparto por provincia, entidad, modalidad y año |
| Recursos (`@`) | `invierte://inversion/{cui}` e `infobras://paralizadas/por-departamento` |
| Prompt (`/`) | `informe_obras(departamento)` |

## Qué se necesita

- [uv](https://docs.astral.sh/uv/getting-started/installation/). La primera vez instala Python 3.12 y las dependencias.
- [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) con la sesión iniciada, para usar el MCP y
  correr la evaluación. Las pruebas no lo necesitan.
- Internet: cada consulta va en vivo a los portales del MEF y de la Contraloría.

## 1. Clonar y correr las pruebas

```bash
git clone https://github.com/IvettexMamani/Proyecto_Obras
cd Proyecto_Obras
uv run pytest -q          # esperado: 8 passed
```

## 2. Conectar el MCP a Claude Code

`.mcp.json` ya trae los dos servidores: `invierte-obras` (`uv run server.py`) y `fetch`. Abre `claude` en la carpeta
del repo y acepta los servidores cuando te lo pregunte. Con `/mcp` deben aparecer los dos conectados. Para probarlos:

| Prueba | En Claude Code | Esperado |
|---|---|---|
| Herramienta propia | «¿Cuánto suman, en soles, las obras paralizadas de Tacna según INFOBRAS?» | Llama a `resumen_obras_departamento` |
| Recurso | `@invierte-obras:invierte://inversion/2192844 ¿Cuántos beneficiarios declara esta inversión?` | 324,498 |
| Prompt | `/mcp__invierte-obras__informe_obras Tacna` | Informe con tres cifras clave y una tabla |
| Fetch | «Lee https://gofastmcp.com/llms.txt y dime qué secciones tiene.» | La lista de secciones |

## 3. Repetir la evaluación

```bash
uv run evaluar.py                              # memoria contra MCP, con Haiku (unos 2 minutos)
uv run evaluar.py --modos memoria web mcp      # agrega la búsqueda web, el experimento extra
uv run grafico.py                              # vuelve a dibujar grafico.png
```

Cada corrida le hace las preguntas a Claude con `claude -p`, con tu cuenta de Claude Code, y escribe `resultados.csv`.

**Las verdades de `preguntas.csv` valen para el 28/10/2026.** Los conteos de INFOBRAS y el devengado cambian a diario,
así que en otra fecha el MCP puede salir «se equivoca» aunque responda bien. Para replicar la evaluación otro día,
revisa cada cifra en el link de la columna `fuente_web`, actualiza `verdad` y vuelve a correr. Si solo cambiaste las
verdades, `uv run evaluar.py --recalificar resultados.csv` vuelve a calificar sin llamar a Claude.

## Archivos

| Archivo | Qué es |
|---|---|
| `server.py` | El servidor MCP |
| `test_server.py` | El contrato del servidor: 8 pruebas con datos reales |
| `.mcp.json` | Los servidores `invierte-obras` y `fetch`, sin claves |
| `preguntas.csv` | Las 10 preguntas, con la verdad, la tolerancia y el link para comprobarla |
| `evaluar.py` | La evaluación memoria / web / MCP |
| `resultados_base.csv`, `resultados.csv` | La corrida base (memoria y MCP) y la del experimento extra (con web) |
| `grafico.py`, `grafico.png` | La figura del informe |
| `informe.md` | Resultados, qué falló cada modo, cifras comprobadas a mano y conclusión |
| `PROCESO.md`, `informe_mcp.ipynb` | Cómo se construyó, paso a paso; el notebook llama al servidor en vivo |

Para abrir el notebook, elige como kernel el `.venv` del proyecto, que `uv run` crea en la primera corrida.

## Si algo falla

| Síntoma | Qué hacer |
|---|---|
| `invalid peer certificate`, `UnknownIssuer` o `CERTIFICATE_VERIFY_FAILED` | Tu red o antivirus inspecciona HTTPS. Antepón `UV_SYSTEM_CERTS=1` a los comandos de `uv` (en PowerShell: `$env:UV_SYSTEM_CERTS=1`). El servidor ya usa los certificados del sistema con `truststore` |
| En Windows, `uv` falla con «El sistema no puede encontrar la ruta especificada» | La ruta de la carpeta es demasiado larga. Clona el repo en una carpeta más corta, por ejemplo `C:\proyectos` |
| `/mcp` muestra `invierte-obras` con error | Corre `uv run pytest -q` para ver el error de Python |
| Una herramienta responde que el portal no contestó | Los portales del MEF y de la Contraloría a veces tardan o se caen. Espera unos segundos y vuelve a intentar |

## Licencia

MIT. Ver [`LICENSE`](LICENSE).
