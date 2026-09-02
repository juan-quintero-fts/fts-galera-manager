from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from urllib.parse import urlencode
import markdown
import re

from .core import settings, inspect_node, classify, recover_position, service_action, bootstrap
from .audit import log, recent, init_db

app = FastAPI(title=settings.app_name)
app.mount('/static', StaticFiles(directory='app/static'), name='static')
templates = Jinja2Templates(directory='app/templates')
init_db()


def ctx(request, **extra):
    base = {
        'request': request,
        'app_name': settings.app_name,
        'ssh_user': settings.ssh_user,
        'root_ssh_user': settings.root_ssh_user,
        'nodes_cfg': settings.nodes,
        'monitor_interval': settings.monitor_interval,
        'auto_monitor': settings.auto_monitor,
    }
    base.update(extra)
    return base


def get_cluster_state():
    nodes = [inspect_node(h) for h in settings.nodes]
    level, summary = classify(nodes)
    return nodes, level, summary


def dashboard_context(request: Request, positions=None, best=None, uuid_warning=False, feedback=None):
    nodes, level, summary = get_cluster_state()
    ssh_up = [n for n in nodes if n['ssh']]
    maria_up = [n for n in nodes if n['mariadb'] == 'active']
    maria_down_accessible = [n for n in nodes if n['ssh'] and n['mariadb'] != 'active']
    primary_nodes = [n for n in maria_up if n['cluster'] == 'Primary']
    all_mariadb_down = len(maria_up) == 0 and len(ssh_up) > 0
    can_join_nodes = len(primary_nodes) > 0 and len(maria_down_accessible) > 0

    return ctx(
        request,
        nodes=nodes,
        level=level,
        summary=summary,
        expected=settings.expected_cluster_size,
        ssh_up_count=len(ssh_up),
        maria_up_count=len(maria_up),
        primary_nodes=primary_nodes,
        maria_down_accessible=maria_down_accessible,
        all_mariadb_down=all_mariadb_down,
        can_join_nodes=can_join_nodes,
        positions=positions,
        best=best,
        uuid_warning=uuid_warning,
        feedback=feedback,
    )


@app.get('/', response_class=HTMLResponse)
def dashboard(request: Request, event: str = '', host: str = '', ok: str = ''):
    feedback = None
    if event and host:
        feedback = {'event': event, 'host': host, 'ok': ok == '1'}
    return templates.TemplateResponse('dashboard.html', dashboard_context(request, feedback=feedback))


@app.get('/api/status')
def api_status():
    """Monitoreo de solo lectura. Nunca ejecuta acciones correctivas."""
    nodes, level, summary = get_cluster_state()
    ssh_up = [n for n in nodes if n['ssh']]
    maria_up = [n for n in nodes if n['mariadb'] == 'active']
    primary_nodes = [n for n in maria_up if n['cluster'] == 'Primary']
    return JSONResponse({
        'level': level,
        'summary': summary,
        'expected': settings.expected_cluster_size,
        'ssh_up_count': len(ssh_up),
        'maria_up_count': len(maria_up),
        'has_primary': len(primary_nodes) > 0,
        'nodes': nodes,
    })


@app.get('/recovery')
def recovery_redirect():
    # La recuperación quedó integrada al Dashboard.
    return RedirectResponse('/#recovery-panel', status_code=303)


@app.post('/recovery/analyze', response_class=HTMLResponse)
def analyze_recovery(request: Request, root_password: str = Form(...)):
    nodes, _, _ = get_cluster_state()
    positions = []

    # El diagnóstico sólo aplica a nodos accesibles con MariaDB detenido.
    for n in nodes:
        if n['ssh'] and n['mariadb'] != 'active':
            try:
                positions.append(recover_position(n['host'], root_password))
            except Exception as e:
                positions.append({
                    'host': n['host'], 'hostname': 'N/A', 'uuid': 'N/A',
                    'seqno': 'N/A', 'source': str(e)
                })

    numeric = [
        p for p in positions
        if str(p['seqno']).lstrip('-').isdigit() and int(p['seqno']) >= 0
    ]
    best = max([int(p['seqno']) for p in numeric], default=None)
    uuids = sorted({p['uuid'] for p in numeric if p['uuid'] != 'N/A'})
    uuid_warning = len(uuids) > 1

    return templates.TemplateResponse(
        'dashboard.html',
        dashboard_context(
            request,
            positions=positions,
            best=best,
            uuid_warning=uuid_warning,
        ),
    )


@app.post('/node/{host}/service')
def node_service(
    host: str,
    action: str = Form(...),
    actor: str = Form('web'),
    root_password: str = Form(...),
):
    if host not in settings.nodes:
        raise HTTPException(404)
    if action != 'start':
        raise HTTPException(400, 'Sólo se permite iniciar/incorporar nodos desde esta operación')

    # Para incorporar un nodo detenido debe existir previamente un Primary activo.
    if action == 'start':
        current_nodes, _, _ = get_cluster_state()
        has_primary = any(
            n['mariadb'] == 'active' and n['cluster'] == 'Primary'
            for n in current_nodes
        )
        if not has_primary:
            raise HTTPException(409, 'No se puede incorporar el nodo porque no existe un Primary activo. Use primero el flujo de recuperación del Dashboard.')

    ok, detail = service_action(host, action, root_password)

    # Validación posterior de estado. No ejecuta ninguna acción adicional.
    after = inspect_node(host)
    detail = (
        f'{detail}\n'
        f'VALIDACIÓN POSTERIOR: MariaDB={after["mariadb"]}, '
        f'Cluster={after["cluster"]}, Ready={after["ready"]}, '
        f'Connected={after["connected"]}, State={after["local_state"]}, Size={after["size"]}'
    )
    action_ok = ok and after['mariadb'] == 'active'
    log(actor, host, f'mariadb:{action}', action_ok, detail)

    query = urlencode({'event': action, 'host': host, 'ok': '1' if action_ok else '0'})
    return RedirectResponse(f'/?{query}', status_code=303)


@app.post('/recovery/bootstrap/{host}')
def do_bootstrap(
    host: str,
    confirm: str = Form(...),
    actor: str = Form('web'),
    root_password: str = Form(...),
):
    if host not in settings.nodes:
        raise HTTPException(404)
    if confirm != 'RECUPERAR':
        raise HTTPException(400, 'Debe escribir RECUPERAR')

    # Protección: el bootstrap sólo está permitido cuando todos los MariaDB accesibles
    # están detenidos. Si existe un MariaDB activo, el operador debe revisar ese estado.
    current_nodes, _, _ = get_cluster_state()
    target = next((n for n in current_nodes if n['host'] == host), None)
    if not target or not target['ssh']:
        raise HTTPException(409, 'El nodo seleccionado no está accesible por SSH.')
    if any(n['mariadb'] == 'active' for n in current_nodes):
        raise HTTPException(409, 'Existe al menos un MariaDB activo. El bootstrap se bloquea para evitar crear un Primary mientras hay otro servicio activo.')

    # Sólo bootstrap manual. Nunca se llama desde monitoreo ni en segundo plano.
    ok, detail = bootstrap(host, root_password)

    # Validar que el nodo quedó realmente como Primary y listo para operar.
    after = inspect_node(host)
    bootstrap_ok = (
        ok
        and after['mariadb'] == 'active'
        and after['cluster'] == 'Primary'
        and after['ready'] in ('ON', '1')
        and after['connected'] in ('ON', '1')
        and after['local_state'] == 'Synced'
    )
    detail = (
        f'{detail}\n'
        f'VALIDACIÓN POSTERIOR: MariaDB={after["mariadb"]}, '
        f'Cluster={after["cluster"]}, Ready={after["ready"]}, '
        f'Connected={after["connected"]}, State={after["local_state"]}, Size={after["size"]}'
    )
    log(actor, host, 'galera:bootstrap', bootstrap_ok, detail)

    query = urlencode({'event': 'bootstrap', 'host': host, 'ok': '1' if bootstrap_ok else '0'})
    return RedirectResponse(f'/?{query}', status_code=303)


@app.get('/audit', response_class=HTMLResponse)
def audit(request: Request):
    return templates.TemplateResponse('audit.html', ctx(request, rows=recent()))


@app.get('/help', response_class=HTMLResponse)
def help_page(request: Request):
    raw = open('docs/AYUDA.md', encoding='utf-8').read()
    mermaids = []

    def stash(m):
        mermaids.append(m.group(1).strip())
        return f'@@MERMAID_{len(mermaids)-1}@@'

    tmp = re.sub(r'```mermaid\s*(.*?)```', stash, raw, flags=re.S)
    html = markdown.markdown(tmp, extensions=['tables', 'fenced_code', 'toc'])
    for i, code in enumerate(mermaids):
        html = html.replace(f'@@MERMAID_{i}@@', f'<div class="mermaid">{code}</div>')
    return templates.TemplateResponse('help.html', ctx(request, content=html))
