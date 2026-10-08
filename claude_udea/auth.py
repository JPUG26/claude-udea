"""
Autenticación en Moodle UdeA sin navegador.
Login directo por HTTP con requests.
Guarda credenciales para re-login automático cuando la sesión expire.
"""

import getpass
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

SESSION_FILE = ".moodle-session.json"
CREDENTIALS_FILE = ".moodle-credentials.json"
KEYRING_SERVICE = "claude-udea"
SESSION_ACCOUNT = "moodle-session"
CREDENTIALS_ACCOUNT = "moodle-credentials"
LOGIN_URL = "https://udearroba.udea.edu.co/internos/login/index.php"
DASHBOARD_URL = "https://udearroba.udea.edu.co/internos/my/"


def _session_path(work_dir: Path) -> Path:
    return work_dir / SESSION_FILE


def _credentials_path(work_dir: Path) -> Path:
    return work_dir / CREDENTIALS_FILE


def _keyring_backend():
    try:
        import keyring
        backend = keyring.get_keyring()
    except Exception as error:
        raise RuntimeError(
            "No se pudo acceder al almacén seguro del sistema. "
            "Las credenciales no se guardarán en archivos sin cifrar."
        ) from error
    if getattr(backend, "priority", 0) <= 0:
        raise RuntimeError(
            "No hay un almacén de credenciales seguro disponible. "
            "Las credenciales no se guardarán en archivos sin cifrar."
        )
    return keyring


def _get_secret(account: str) -> str | None:
    return _keyring_backend().get_password(KEYRING_SERVICE, account)


def _set_secret(account: str, value: str):
    keyring = _keyring_backend()
    keyring.set_password(KEYRING_SERVICE, account, value)
    if keyring.get_password(KEYRING_SERVICE, account) != value:
        raise RuntimeError("El almacén seguro no confirmó la escritura del secreto.")


def save_session(session: requests.Session, work_dir: Path):
    """Guarda cookies en el almacén seguro del sistema operativo."""
    data = []
    for cookie in session.cookies:
        data.append({
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path,
        })
    _set_secret(SESSION_ACCOUNT, json.dumps(data))
    _session_path(work_dir).unlink(missing_ok=True)


def _save_credentials(work_dir: Path, username: str, password: str):
    """Guarda credenciales cifradas por el almacén del sistema operativo."""
    _set_secret(
        CREDENTIALS_ACCOUNT,
        json.dumps({"username": username, "password": password}),
    )
    _credentials_path(work_dir).unlink(missing_ok=True)


def _load_credentials(work_dir: Path) -> tuple[str, str] | None:
    """Carga credenciales guardadas."""
    try:
        stored = _get_secret(CREDENTIALS_ACCOUNT)
        path = _credentials_path(work_dir)
        if stored is not None:
            data = json.loads(stored)
        elif path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            return None
        return data.get("username", ""), data.get("password", "")
    except (OSError, ValueError, TypeError):
        return None


def migrate_saved_secrets(work_dir: Path) -> list[str]:
    """Migra archivos JSON legados al almacén seguro y los elimina tras verificarlos."""
    migrated = []
    legacy_secrets = (
        (_session_path(work_dir), SESSION_ACCOUNT),
        (_credentials_path(work_dir), CREDENTIALS_ACCOUNT),
    )
    for path, account in legacy_secrets:
        if not path.exists():
            continue
        legacy_value = path.read_text(encoding="utf-8")
        stored = _get_secret(account)
        if stored is None:
            _set_secret(account, legacy_value)
            stored = _get_secret(account)
        if stored != legacy_value:
            raise RuntimeError(
                f"No se pudo verificar la migración segura de {path.name}; "
                "el archivo original se conservó."
            )
        path.unlink()
        migrated.append(path.name)
    return migrated


def load_session(work_dir: Path) -> requests.Session | None:
    """Carga sesión guardada y verifica que siga activa."""
    path = _session_path(work_dir)
    stored = _get_secret(SESSION_ACCOUNT)
    if stored is None and not path.exists():
        return None

    try:
        if stored is not None:
            cookies = json.loads(stored)
        elif path.exists():
            with open(path, "r", encoding="utf-8") as f:
                cookies = json.load(f)
        else:
            return None
    except (OSError, ValueError, TypeError):
        return None

    session = requests.Session()
    for c in cookies:
        session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])

    # Verificar que la sesión siga activa (seguir redirects para manejar SSO)
    try:
        r = session.get(DASHBOARD_URL, allow_redirects=True, timeout=15)
        if "login" in r.url.lower():
            return None
        if r.status_code == 200:
            return session
    except Exception:
        pass

    return None


def _do_login_request(username: str, password: str) -> requests.Session:
    """Hace login HTTP POST y retorna la sesión autenticada."""
    session = requests.Session()

    # Obtener logintoken del formulario
    r = session.get(LOGIN_URL, timeout=15)
    soup = BeautifulSoup(r.text, "html.parser")
    token_input = soup.find("input", {"name": "logintoken"})
    logintoken = token_input["value"] if token_input else ""

    # POST login
    r = session.post(LOGIN_URL, data={
        "anchor": "",
        "logintoken": logintoken,
        "username": username,
        "password": password,
    }, allow_redirects=True, timeout=15)

    # Verificar login exitoso
    if "login" in r.url.lower() and "errorcode" not in r.url:
        soup = BeautifulSoup(r.text, "html.parser")
        error = soup.find("div", {"class": "alert-danger"}) or soup.find("div", {"id": "loginerrormessage"})
        if error:
            raise ValueError(f"Login fallido: {error.get_text(strip=True)}")
        if "login/index.php" in r.url:
            raise ValueError("Login fallido: credenciales incorrectas")

    # Verificar acceso al dashboard
    r = session.get(DASHBOARD_URL, timeout=15)
    if "login" in r.url.lower():
        raise ValueError("Login fallido: no se pudo acceder al dashboard")

    return session


def login(work_dir: Path, username: str = None, password: str = None) -> requests.Session:
    """
    Intenta restaurar sesión guardada. Si expiró, re-login automático
    con credenciales guardadas. Solo pide credenciales la primera vez.
    """
    # 1. Intentar sesión guardada
    session = load_session(work_dir)
    if session:
        print("  ✔ Sesión activa\n")
        return session

    # 2. Intentar re-login con credenciales guardadas
    if not username and not password:
        saved = _load_credentials(work_dir)
        if saved:
            saved_user, saved_pass = saved
            if saved_user and saved_pass:
                try:
                    session = _do_login_request(saved_user, saved_pass)
                    save_session(session, work_dir)
                    print("  ✔ Re-login automático exitoso\n")
                    return session
                except ValueError:
                    # Credenciales guardadas ya no sirven, pedir nuevas
                    print("  ⚠ Credenciales guardadas inválidas, pidiendo nuevas...\n")

    # 3. Pedir credenciales manualmente
    if not username:
        print()
        username = input("  Usuario Moodle UdeA: ").strip()
    if not password:
        password = getpass.getpass("  Contraseña: ")

    session = _do_login_request(username, password)
    save_session(session, work_dir)
    _save_credentials(work_dir, username, password)
    print("  ✔ Login exitoso, credenciales guardadas\n")
    return session


def _ingenia_recordings_from_html(html: str) -> list[dict]:
    """Lee el arreglo completo de grabaciones serializado por Next.js."""
    soup = BeautifulSoup(html, "html.parser")
    decoder = json.JSONDecoder()

    for script in soup.find_all("script"):
        script_text = script.string or script.get_text()
        match = re.search(r"self\.__next_f\.push\((.*)\);?\s*$", script_text, re.DOTALL)
        if not match:
            continue

        try:
            push_data = json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(push_data, list) or len(push_data) < 2:
            continue

        payload = push_data[1]
        if not isinstance(payload, str):
            continue
        marker = re.search(r'"recordings"\s*:\s*\[', payload)
        if not marker:
            continue

        array_start = payload.find("[", marker.start())
        try:
            recordings, _ = decoder.raw_decode(payload, array_start)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(recordings, list) and recordings:
            return recordings

    return []


def _scrape_ingenia(session: requests.Session, slug: str,
                    course_info: dict) -> tuple[str, list[dict]]:
    """Extrae todas las grabaciones de una página de Virtual Ingeniería."""
    last_error = None
    recordings = []
    for attempt in range(3):
        try:
            response = session.get(
                course_info["moodle_url"],
                headers={"Cache-Control": "no-cache"},
                timeout=30,
            )
            response.raise_for_status()
            if "login" in urlparse(response.url).path.lower():
                raise PermissionError("Ingenia redirigió a una página de acceso.")
            recordings = _ingenia_recordings_from_html(response.text)
            if recordings:
                break
            last_error = "la respuesta no contenía el bloque de grabaciones de Next.js"
        except Exception as error:
            last_error = str(error)
        if attempt < 2:
            time.sleep(0.5 * (attempt + 1))

    if not recordings:
        print(
            f"  ⚠ No se encontró metadata de grabaciones en {course_info['name']} "
            f"(Ingenia) después de 3 intentos: {last_error}"
        )
        return slug, []

    links = []
    seen = set()
    for recording in recordings:
        video_url = recording.get("videoUrl", "")
        if not video_url:
            continue

        match = re.search(r"/rec/(?:share|play)/([^?\s]+)", video_url)
        rec_id = match.group(1) if match else video_url
        if rec_id in seen:
            continue
        seen.add(rec_id)

        start_date = recording.get("startTime", "")
        if isinstance(start_date, str) and start_date.startswith("$D"):
            start_date = start_date[2:]

        title = recording.get("topic") or course_info["name"]
        links.append({
            "url": video_url.split("?")[0],
            "full_url": video_url,
            "text": title,
            "id": rec_id,
            "meeting_id": recording.get("id", ""),
            "topic": title,
            "start_date": start_date,
            "duration_minutes": int(recording.get("duration") or 0),
        })

    return slug, links


def _scrape_one(session: requests.Session, slug: str, course_info: dict) -> tuple[str, list[dict]]:
    """Scrapea una materia. Diseñado para correr en un thread."""
    url = course_info["moodle_url"]
    if urlparse(url).hostname == "ingenia.udea.edu.co":
        return _scrape_ingenia(session, slug, course_info)

    try:
        r = session.get(url, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"  ⚠ Error accediendo a {course_info['name']}: {e}")
        return slug, []

    # Verificar que no nos redirigió al login
    if "login" in r.url.lower():
        print(f"  ⚠ Sesión expirada al acceder a {course_info['name']}")
        return slug, []

    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table", class_="generaltable")
    if not table:
        return slug, []

    tbody = table.find("tbody")
    if not tbody:
        return slug, []

    links = []
    seen = set()

    for row in tbody.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        meeting_id = cells[0].get_text(strip=True)
        topic = cells[1].get_text(strip=True)
        start_date = cells[2].get_text(strip=True)
        duration = cells[3].get_text(strip=True)

        hidden_input = row.find("input", {"name": "zoomplayredirect"})
        if not hidden_input:
            # Grabación visible en Moodle pero sin URL (aún procesándose en Zoom)
            print(f"  ⚠ {course_info['name']}: grabación del {start_date} sin URL (procesándose en Zoom)")
            continue

        href = hidden_input.get("value", "")
        if not href:
            print(f"  ⚠ {course_info['name']}: grabación del {start_date} con URL vacía")
            continue

        match = re.search(r"/rec/(?:share|play)/([^?\s]+)", href)
        rec_id = match.group(1) if match else href
        if rec_id in seen:
            continue
        seen.add(rec_id)

        links.append({
            "url": href.split("?")[0],
            "full_url": href,
            "text": topic or meeting_id,
            "id": rec_id,
            "meeting_id": meeting_id,
            "topic": topic,
            "start_date": start_date,
            "duration_minutes": int(duration) if duration.isdigit() else 0,
        })

    return slug, links


def scrape_recordings(session: requests.Session, courses: dict) -> dict:
    """
    Scrapea todas las materias en paralelo con ThreadPoolExecutor.
    Retorna {slug: [links]}.
    """
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(courses)) as pool:
        futures = {
            pool.submit(_scrape_one, session, slug, info): slug
            for slug, info in courses.items()
        }
        results = {}
        for future in futures:
            slug, links = future.result()
            results[slug] = links

    return results
