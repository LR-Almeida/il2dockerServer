import os
import pty
import re
import glob
import shutil
import subprocess
import threading
import time
from functools import wraps
from flask import (
    Flask, render_template, request, jsonify,
    redirect, url_for, flash, Response,
)
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.urandom(32)
app.config['MAX_CONTENT_LENGTH'] = 512 * 1024 * 1024  # 512 MB max upload
app.config['TEMPLATES_AUTO_RELOAD'] = True

# --- Environment config --------------------------------------------------

IL2_PATH  = os.environ.get('IL2_PATH', '/il2')
SDS_PATH  = os.environ.get('SDS_PATH', os.path.join(IL2_PATH, 'data', 'server.sds'))
DATA_PATH   = os.path.join(IL2_PATH, 'data', 'Multiplayer')  # SDS rotation paths are relative to this
BACKUP_PATH = os.path.join(IL2_PATH, 'data', 'mission_backup')  # backed-up .mission files when .msnbin is active
LOG_PATH    = os.path.join(IL2_PATH, 'logs', 'dserver.log')
DSERVER_EXE = os.path.join(IL2_PATH, 'bin', 'game', 'DServer.exe')

WEB_USER    = os.environ.get('WEB_USER', 'admin')
WEB_PASS    = os.environ.get('WEB_PASS', 'il2admin')
AUTO_START  = os.environ.get('AUTO_START', 'false').lower() == 'true'
WINEPREFIX  = os.environ.get('WINEPREFIX', '/opt/wineprefix')

# --- Server process management -------------------------------------------

_proc       = None
_pty_master = None   # master end of PTY kept open so wine's console doesn't get SIGHUP
_proc_lock  = threading.Lock()


def _pgrep_dserver():
    """Return PIDs of any running DServer process (patched or original)."""
    try:
        result = subprocess.run(
            ['pgrep', '-f', 'DServer'],  # matches DServer.exe and DServer_patched.exe
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return [int(p) for p in result.stdout.split() if p.strip().isdigit()]
    except Exception:
        pass
    return []


def _kill_wineserver():
    """Kill the wineserver for WINEPREFIX — instantly terminates all Wine processes."""
    try:
        subprocess.run(
            ['wineserver', '-k'],
            env={**os.environ, 'WINEPREFIX': WINEPREFIX},
            timeout=5,
        )
    except Exception:
        pass


def server_status():
    with _proc_lock:
        if _proc and _proc.poll() is None:
            return 'running'
    if _pgrep_dserver():
        return 'running'
    return 'stopped'


def server_pid():
    with _proc_lock:
        if _proc and _proc.poll() is None:
            return _proc.pid
    pids = _pgrep_dserver()
    return pids[0] if pids else None


def do_start():
    global _proc, _pty_master
    with _proc_lock:
        if _proc and _proc.poll() is None:
            return False, 'Server is already running.'
        if not os.path.isfile(DSERVER_EXE):
            return False, f'DServer executable not found at {DSERVER_EXE}. Check your volume mount.'
        if not os.path.isfile(SDS_PATH):
            return False, f'SDS file not found at {SDS_PATH}. Create it via the Config page.'
        env = os.environ.copy()
        env['DISPLAY']    = ':99'
        env['HOME']       = '/root'
        env['WINEPREFIX'] = WINEPREFIX
        env['WINEDEBUG']  = 'warn+err'
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        log_fd = open(LOG_PATH, 'a')
        # Give wine a real PTY on stdin so its console subsystem initialises
        # correctly. Without a TTY, wine's console init fails and DServer hits
        # a null-pointer that was masked by the binary patch (causing -1 players).
        master_fd, slave_fd = pty.openpty()
        try:
            _proc = subprocess.Popen(
                ['wine', DSERVER_EXE, SDS_PATH],
                stdin=slave_fd,
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=os.path.dirname(DSERVER_EXE),
            )
        finally:
            os.close(slave_fd)
        if _pty_master is not None:
            try:
                os.close(_pty_master)
            except OSError:
                pass
        _pty_master = master_fd
        return True, f'Server started (PID {_proc.pid}).'


def do_stop():
    global _proc, _pty_master
    with _proc_lock:
        already_stopped = not _pgrep_dserver() and not (_proc and _proc.poll() is None)
        if already_stopped:
            _proc = None
            return False, 'Server is not running.'

        # Step 1: politely ask via _proc if we have a live reference
        if _proc and _proc.poll() is None:
            try:
                _proc.terminate()
                _proc.wait(timeout=5)
            except Exception:
                pass
            _proc = None

        # Step 2: SIGKILL any surviving DServer processes (Wine ignores SIGTERM)
        for pid in _pgrep_dserver():
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass

        # Step 3: kill the wineserver — guarantees all Wine processes in the prefix die
        _kill_wineserver()

        if _pty_master is not None:
            try:
                os.close(_pty_master)
            except OSError:
                pass
            _pty_master = None

        time.sleep(1)
        if _pgrep_dserver():
            return False, 'Server could not be stopped — kill it manually.'
        return True, 'Server stopped.'


# --- SDS parser / writer -------------------------------------------------

# Fields whose values must be written as quoted strings in the SDS file
_STRING_FIELDS = {
    'login', 'password', 'ServerName', 'serverDesc', 'srsCommServer',
    'protection', 'ServerIP', 'RconIP', 'RconLogin', 'RconPassword',
}

# All known boolean fields (written as true/false, not 0/1)
_BOOL_FIELDS = {
    'TacviewRecord', 'AllowExtCamSpectator', 'AllowExtCamPlayer',
    'coalitionsBalancer', 'allowMarshals', 'useMarshalsRestriction',
    'objectIcons', 'navigationIcons', 'aimingHelp', 'courseWeaponsAimingHelp',
    'padlock', 'simpleDevices', 'techChatMessages', 'techChatAdvices',
    'easyFlight', 'autoCoordination', 'autoThrottle', 'autoPilot',
    'autoThrottleLimit', 'autoMix', 'autoRadiator', 'noMoment', 'noWind',
    'noMisfire', 'noBreak', 'invulnerability', 'simplePhysiology',
    'unlimitFuel', 'unlimitAmmo', 'engineNoStop', 'hotEngine', 'alterVisibility',
}


def _parse_raw_value(raw):
    """Convert a raw string token to a Python value."""
    s = raw.strip().strip('"')
    if raw.strip().lower() == 'true':
        return True
    if raw.strip().lower() == 'false':
        return False
    try:
        return int(s)
    except ValueError:
        pass
    return s


def _format_value(key, val):
    """Format a Python value back to the SDS wire format."""
    if key in _STRING_FIELDS:
        return f'"{val}"'
    if isinstance(val, bool) or key in _BOOL_FIELDS:
        if isinstance(val, str):
            val = val.lower() in ('true', '1', 'yes')
        return 'true' if val else 'false'
    return str(val)


def sds_load(path=None):
    """
    Parse an SDS file.  Returns a dict:
        {
          'values':    {key: value, ...},           # top-level key/value pairs
          'rotation':  {
              'random':    bool,
              'missions':  [{path, turntime, checkairfields, refighttime}, ...]
          }
        }
    """
    if path is None:
        path = SDS_PATH
    data = {
        'values': {},
        'rotation': {'random': False, 'missions': []},
    }
    in_rotation = False
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith('//'):
                    continue
                if line == '[rotation]':
                    in_rotation = True
                    continue
                if line == '[end]':
                    in_rotation = False
                    continue

                if in_rotation:
                    if '=' in line:
                        k, _, v = line.partition('=')
                        k = k.strip()
                        v = v.strip().strip('"')
                        if k == 'random':
                            data['rotation']['random'] = v.lower() == 'true'
                        elif k == 'file':
                            # Format: file = "Cooperative\MissionName"
                            # Path is relative to data/, uses backslashes, no extension
                            data['rotation']['missions'].append({'path': v})
                else:
                    if '=' in line:
                        k, _, v = line.partition('=')
                        data['values'][k.strip()] = _parse_raw_value(v)
    except FileNotFoundError:
        pass
    return data


def sds_save(data, path=None):
    """Write an SDS dict back to disk, preserving the standard section order."""
    if path is None:
        path = SDS_PATH
    v   = data['values']
    rot = data['rotation']

    def fv(key, default=''):
        return _format_value(key, v.get(key, default))

    lines = [
        '// Generated by IL-2 Server Manager\n',
        '\n// credentials\n\n',
        f'login = {fv("login")}\n',
        f'password = {fv("password")}\n',
        '\n// server info\n\n',
        f'ranked = {fv("ranked", 0)}\n',
        f'mode = {fv("mode", 0)}\n',
        f'banTimeout = {fv("banTimeout", 900)}\n',
        f'lobbyTimer = {fv("lobbyTimer", 30)}\n',
        f'coopQuorum = {fv("coopQuorum", 0)}\n',
        f'allowMouseJoy = {fv("allowMouseJoy", 1)}\n',
        f'ServerName = {fv("ServerName")}\n',
        f'TacviewRecord = {fv("TacviewRecord", False)}\n',
        f'\nserverDesc = {fv("serverDesc")}\n',
        '\n// SRS comm server IP:Port ("255.255.255.255:1234") or URL:Port\n',
        f'srsCommServer = {fv("srsCommServer")}\n',
        '\n// connection settings\n\n',
        f'protection = {fv("protection")}\n',
        f'maxClients = {fv("maxClients", 10)}\n',
        f'maxClientPing = {fv("maxClientPing", -1)}\n',
        f'ExternalIP = {fv("ExternalIP", 1)}\n',
        f'ServerIP = {fv("ServerIP")}\n',
        f'DownloadLimit = {fv("DownloadLimit", 50000)}\n',
        f'UploadLimit = {fv("UploadLimit", 50000)}\n',
        f'DownloaderPort = {fv("DownloaderPort", 28100)}\n',
        f'TCPPort = {fv("TCPPort", 28000)}\n',
        f'UDPPort = {fv("UDPPort", 28000)}\n',
        '\n// remote console settings\n\n',
        f'RconStart = {fv("RconStart", 0)}\n',
        f'RconIP = {fv("RconIP")}\n',
        f'RconPort = {fv("RconPort", 8991)}\n',
        f'RconLogin = {fv("RconLogin")}\n',
        f'RconPassword = {fv("RconPassword")}\n',
        '\n// mission rotation data\n\n',
        f'ShutdownLoads = {fv("ShutdownLoads", -1)}\n',
        '\n[rotation]\n',
        f'random = {"true" if rot["random"] else "false"}\n',
    ]
    for mis in rot['missions']:
        # Format: file = "Cooperative\MissionName"  (path relative to data/, backslash, no extension)
        lines.append(f'   file = "{mis["path"]}"\n')
    lines += [
        '[end]\n',
        '\n// preset and advanced settings\n\n',
        f'preset = {fv("preset", 1)}\n',
        '\n// preset: server related\n\n',
        f'killNotification = {fv("killNotification", 1)}\n',
        f'friendlyFireReturn = {fv("friendlyFireReturn", 0)}\n',
        f'finishMissionIfLanded = {fv("finishMissionIfLanded", 0)}\n',
        f'lockPayloads = {fv("lockPayloads", 0)}\n',
        f'lockSkins = {fv("lockSkins", 0)}\n',
        f'lockFuelLoads = {fv("lockFuelLoads", 0)}\n',
        f'lockWeaponModes = {fv("lockWeaponModes", 0)}\n',
        f'lockPlayerTankAIaimAtObj = {fv("lockPlayerTankAIaimAtObj", 0)}\n',
        f'lockInjectors = {fv("lockInjectors", 0)}\n',
        f'AllowExtCamSpectator = {fv("AllowExtCamSpectator", True)}\n',
        f'penaltyTimeout = {fv("penaltyTimeout", 10)}\n',
        f'respawnTimeout = {fv("respawnTimeout", 0)}\n',
        f'coalitionChangeTimeout = {fv("coalitionChangeTimeout", 10)}\n',
        f'finishMissionTimeout = {fv("finishMissionTimeout", 0)}\n',
        f'missionEndTimeout = {fv("missionEndTimeout", 0)}\n',
        f'idleKickTimeout = {fv("idleKickTimeout", 0)}\n',
        f'tdmPointsPerRound = {fv("tdmPointsPerRound", 0)}\n',
        f'tdmRoundTime = {fv("tdmRoundTime", 0)}\n',
        f'coalitionsBalancer = {fv("coalitionsBalancer", False)}\n',
        f'allowMarshals = {fv("allowMarshals", False)}\n',
        f'useMarshalsRestriction = {fv("useMarshalsRestriction", False)}\n',
        '\n// preset: mission related\n\n',
        f'objectIcons = {fv("objectIcons", True)}\n',
        f'navigationIcons = {fv("navigationIcons", True)}\n',
        f'aimingHelp = {fv("aimingHelp", False)}\n',
        f'courseWeaponsAimingHelp = {fv("courseWeaponsAimingHelp", False)}\n',
        f'padlock = {fv("padlock", True)}\n',
        f'simpleDevices = {fv("simpleDevices", True)}\n',
        f'techChatMessages = {fv("techChatMessages", True)}\n',
        f'techChatAdvices = {fv("techChatAdvices", False)}\n',
        f'AllowExtCamPlayer = {fv("AllowExtCamPlayer", True)}\n',
        f'\neasyFlight = {fv("easyFlight", False)}\n',
        f'autoCoordination = {fv("autoCoordination", False)}\n',
        f'autoThrottle = {fv("autoThrottle", False)}\n',
        f'autoPilot = {fv("autoPilot", True)}\n',
        f'autoThrottleLimit = {fv("autoThrottleLimit", True)}\n',
        f'autoMix = {fv("autoMix", True)}\n',
        f'autoRadiator = {fv("autoRadiator", True)}\n',
        f'\nnoMoment = {fv("noMoment", False)}\n',
        f'noWind = {fv("noWind", False)}\n',
        f'noMisfire = {fv("noMisfire", False)}\n',
        f'noBreak = {fv("noBreak", False)}\n',
        f'invulnerability = {fv("invulnerability", False)}\n',
        f'simplePhysiology = {fv("simplePhysiology", False)}\n',
        f'unlimitFuel = {fv("unlimitFuel", False)}\n',
        f'unlimitAmmo = {fv("unlimitAmmo", False)}\n',
        f'engineNoStop = {fv("engineNoStop", False)}\n',
        f'hotEngine = {fv("hotEngine", True)}\n',
        f'alterVisibility = {fv("alterVisibility", False)}\n',
    ]

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.writelines(lines)


# --- Mission file helpers -------------------------------------------------

def list_missions():
    """
    Scan DATA_PATH for .Mission and .msnbin files, and BACKUP_PATH for backed-up
    .Mission files. Returns one entry per unique SDS path.
    """
    # keyed by sds_path.lower() → {sds_path, mission_abs, msnbin_abs, backup_abs}
    index = {}

    def _add(sds_path, mission_abs=None, msnbin_abs=None, backup_abs=None):
        key = sds_path.lower()
        if key not in index:
            index[key] = {'sds_path': sds_path, 'mission_abs': None,
                          'msnbin_abs': None, 'backup_abs': None}
        if mission_abs:
            index[key]['mission_abs'] = mission_abs
        if msnbin_abs:
            index[key]['msnbin_abs'] = msnbin_abs
        if backup_abs:
            index[key]['backup_abs'] = backup_abs

    # Active directory: .mission and .msnbin files
    for root, _dirs, files in os.walk(DATA_PATH):
        for fname in files:
            fl = fname.lower()
            fpath = os.path.join(root, fname)
            if fl.endswith('.mission'):
                rel = os.path.relpath(fpath, DATA_PATH)
                sds = os.path.splitext(rel)[0].replace('/', '\\')
                _add(sds, mission_abs=fpath)
            elif fl.endswith('.msnbin'):
                rel = os.path.relpath(fpath, DATA_PATH)
                sds = os.path.splitext(rel)[0].replace('/', '\\')
                _add(sds, msnbin_abs=fpath)

    # Backup directory: backed-up .mission files
    if os.path.isdir(BACKUP_PATH):
        for root, _dirs, files in os.walk(BACKUP_PATH):
            for fname in files:
                if not fname.lower().endswith('.mission'):
                    continue
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, BACKUP_PATH)
                sds = os.path.splitext(rel)[0].replace('/', '\\')
                _add(sds, backup_abs=fpath)

    result = []
    for key in sorted(index.keys()):
        e = index[key]
        sds_path = e['sds_path']
        lower    = sds_path.lower()
        mode     = 'Cooperative' if 'cooperative' in lower else 'Dogfight'
        name     = sds_path.split('\\')[-1]

        has_msnbin       = e['msnbin_abs'] is not None
        mission_backed_up = e['backup_abs'] is not None and e['mission_abs'] is None

        # Active directory for companion file scanning
        if e['msnbin_abs']:
            active_dir = os.path.dirname(e['msnbin_abs'])
            abs_ref    = e['msnbin_abs']
        elif e['mission_abs']:
            active_dir = os.path.dirname(e['mission_abs'])
            abs_ref    = e['mission_abs']
        else:
            active_dir = None
            abs_ref    = e['backup_abs']

        # Companion file scan (language / .list detection)
        stem = name.lower()
        has_list = False
        has_lang = False
        lang_exts = []
        if active_dir and os.path.isdir(active_dir):
            for entry in os.scandir(active_dir):
                if not entry.is_file():
                    continue
                e_stem, e_ext = os.path.splitext(entry.name)
                if e_stem.lower() != stem:
                    continue
                ext_lower = e_ext.lower()
                if ext_lower == '.list':
                    has_list = True
                elif ext_lower not in ('.mission', '.msnbin'):
                    has_lang = True
                    lang_exts.append(e_ext)

        result.append({
            'name':              name,
            'path':              sds_path,
            'abs':               abs_ref,
            'mode':              mode,
            'has_msnbin':        has_msnbin,
            'mission_backed_up': mission_backed_up,
            'warn_missing_list': has_lang and not has_list,
            'lang_exts':         sorted(set(lang_exts)),
        })
    return result


def find_mission_file(sds_path):
    """Return the active .Mission file path, or None if not in DATA_PATH."""
    base = os.path.join(DATA_PATH, sds_path.replace('\\', os.sep))
    for ext in ('.Mission', '.mission', '.MISSION'):
        candidate = base + ext
        if os.path.isfile(candidate):
            return candidate
    return None


def find_msnbin_file(sds_path):
    """Return the active .msnbin file path, or None if not in DATA_PATH."""
    base = os.path.join(DATA_PATH, sds_path.replace('\\', os.sep))
    for ext in ('.msnbin', '.Msnbin', '.MSNBIN'):
        candidate = base + ext
        if os.path.isfile(candidate):
            return candidate
    return None


def find_backup_mission_file(sds_path):
    """Return the backed-up .Mission file path in BACKUP_PATH, or None."""
    base = os.path.join(BACKUP_PATH, sds_path.replace('\\', os.sep))
    for ext in ('.Mission', '.mission', '.MISSION'):
        candidate = base + ext
        if os.path.isfile(candidate):
            return candidate
    return None


# --- HTTP Basic Auth -----------------------------------------------------

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != WEB_USER or auth.password != WEB_PASS:
            return Response('Login required.', 401,
                            {'WWW-Authenticate': 'Basic realm="IL-2 Server Manager"'})
        return f(*args, **kwargs)
    return decorated


# --- Routes --------------------------------------------------------------

@app.route('/')
@requires_auth
def index():
    data = sds_load()
    v    = data['values']
    return render_template('index.html',
                           status=server_status(),
                           pid=server_pid(),
                           server_name=v.get('ServerName', '—'),
                           tcp_port=v.get('TCPPort', '28000'),
                           udp_port=v.get('UDPPort', '28000'),
                           dl_port=v.get('DownloaderPort', '28100'),
                           max_clients=v.get('maxClients', '—'),
                           sds_path=SDS_PATH)


@app.route('/server/start', methods=['POST'])
@requires_auth
def route_start():
    ok, msg = do_start()
    flash(msg, 'success' if ok else 'danger')
    return redirect(url_for('index'))


@app.route('/server/stop', methods=['POST'])
@requires_auth
def route_stop():
    ok, msg = do_stop()
    flash(msg, 'success' if ok else 'danger')
    return redirect(url_for('index'))


@app.route('/server/restart', methods=['POST'])
@requires_auth
def route_restart():
    do_stop()
    time.sleep(3)
    ok, msg = do_start()
    flash(msg, 'success' if ok else 'danger')
    return redirect(url_for('index'))


@app.route('/config', methods=['GET', 'POST'])
@requires_auth
def config():
    data = sds_load()
    v    = data['values']

    if request.method == 'POST':
        f = request.form

        def chk(name):
            return f.get(name) is not None

        # Credentials
        v['login']    = f.get('login', v.get('login', ''))
        if f.get('password'):
            v['password'] = f.get('password')

        # Server info
        v['ServerName']     = f.get('ServerName', v.get('ServerName', ''))
        v['serverDesc']     = f.get('serverDesc', v.get('serverDesc', ''))
        v['mode']           = int(f.get('mode', v.get('mode', 0)))
        v['ranked']         = int(f.get('ranked', v.get('ranked', 0)))
        v['maxClients']     = int(f.get('maxClients', v.get('maxClients', 10)))
        v['protection']     = f.get('protection', v.get('protection', ''))
        v['banTimeout']     = int(f.get('banTimeout', v.get('banTimeout', 900)))
        v['lobbyTimer']     = int(f.get('lobbyTimer', v.get('lobbyTimer', 30)))
        v['coopQuorum']     = int(f.get('coopQuorum', v.get('coopQuorum', 0)))
        v['allowMouseJoy']  = int(chk('allowMouseJoy'))
        v['TacviewRecord']  = chk('TacviewRecord')
        v['srsCommServer']  = f.get('srsCommServer', v.get('srsCommServer', ''))
        v['ShutdownLoads']  = int(f.get('ShutdownLoads', v.get('ShutdownLoads', -1)))

        # Connection
        v['ServerIP']       = f.get('ServerIP', v.get('ServerIP', ''))
        v['ExternalIP']     = int(chk('ExternalIP'))
        v['TCPPort']        = int(f.get('TCPPort', v.get('TCPPort', 28000)))
        v['UDPPort']        = int(f.get('UDPPort', v.get('UDPPort', 28000)))
        v['DownloaderPort'] = int(f.get('DownloaderPort', v.get('DownloaderPort', 28100)))
        v['DownloadLimit']  = int(f.get('DownloadLimit', v.get('DownloadLimit', 50000)))
        v['UploadLimit']    = int(f.get('UploadLimit', v.get('UploadLimit', 50000)))
        v['maxClientPing']  = int(f.get('maxClientPing', v.get('maxClientPing', -1)))

        # RCON
        v['RconStart']    = int(chk('RconStart'))
        v['RconIP']       = f.get('RconIP', v.get('RconIP', ''))
        v['RconPort']     = int(f.get('RconPort', v.get('RconPort', 8991)))
        v['RconLogin']    = f.get('RconLogin', v.get('RconLogin', ''))
        if f.get('RconPassword'):
            v['RconPassword'] = f.get('RconPassword')

        # Preset / server
        v['preset']                   = int(f.get('preset', v.get('preset', 1)))
        v['killNotification']         = int(chk('killNotification'))
        v['friendlyFireReturn']       = int(chk('friendlyFireReturn'))
        v['finishMissionIfLanded']    = int(chk('finishMissionIfLanded'))
        v['lockPayloads']             = int(chk('lockPayloads'))
        v['lockSkins']                = int(chk('lockSkins'))
        v['lockFuelLoads']            = int(chk('lockFuelLoads'))
        v['lockWeaponModes']          = int(chk('lockWeaponModes'))
        v['lockPlayerTankAIaimAtObj'] = int(chk('lockPlayerTankAIaimAtObj'))
        v['lockInjectors']            = int(chk('lockInjectors'))
        v['AllowExtCamSpectator']     = chk('AllowExtCamSpectator')
        v['AllowExtCamPlayer']        = chk('AllowExtCamPlayer')
        v['penaltyTimeout']           = int(f.get('penaltyTimeout', 10))
        v['respawnTimeout']           = int(f.get('respawnTimeout', 0))
        v['coalitionChangeTimeout']   = int(f.get('coalitionChangeTimeout', 10))
        v['finishMissionTimeout']     = int(f.get('finishMissionTimeout', 0))
        v['missionEndTimeout']        = int(f.get('missionEndTimeout', 0))
        v['idleKickTimeout']          = int(f.get('idleKickTimeout', 0))
        v['tdmPointsPerRound']        = int(f.get('tdmPointsPerRound', 0))
        v['tdmRoundTime']             = int(f.get('tdmRoundTime', 0))
        v['coalitionsBalancer']       = chk('coalitionsBalancer')
        v['allowMarshals']            = chk('allowMarshals')
        v['useMarshalsRestriction']   = chk('useMarshalsRestriction')

        # Preset / mission
        v['objectIcons']             = chk('objectIcons')
        v['navigationIcons']         = chk('navigationIcons')
        v['aimingHelp']              = chk('aimingHelp')
        v['courseWeaponsAimingHelp'] = chk('courseWeaponsAimingHelp')
        v['padlock']                 = chk('padlock')
        v['simpleDevices']           = chk('simpleDevices')
        v['techChatMessages']        = chk('techChatMessages')
        v['techChatAdvices']         = chk('techChatAdvices')
        v['easyFlight']              = chk('easyFlight')
        v['autoCoordination']        = chk('autoCoordination')
        v['autoThrottle']            = chk('autoThrottle')
        v['autoPilot']               = chk('autoPilot')
        v['autoThrottleLimit']       = chk('autoThrottleLimit')
        v['autoMix']                 = chk('autoMix')
        v['autoRadiator']            = chk('autoRadiator')
        v['noMoment']                = chk('noMoment')
        v['noWind']                  = chk('noWind')
        v['noMisfire']               = chk('noMisfire')
        v['noBreak']                 = chk('noBreak')
        v['invulnerability']         = chk('invulnerability')
        v['simplePhysiology']        = chk('simplePhysiology')
        v['unlimitFuel']             = chk('unlimitFuel')
        v['unlimitAmmo']             = chk('unlimitAmmo')
        v['engineNoStop']            = chk('engineNoStop')
        v['hotEngine']               = chk('hotEngine')
        v['alterVisibility']         = chk('alterVisibility')

        sds_save(data)
        flash('Configuration saved. Restart the server to apply changes.', 'success')
        return redirect(url_for('config'))

    return render_template('config.html', v=v)


@app.route('/missions', methods=['GET', 'POST'])
@requires_auth
def missions():
    data = sds_load()
    rot  = data['rotation']

    if request.method == 'POST':
        enabled = request.form.getlist('mission')
        rot['random']   = request.form.get('random') is not None
        rot['missions'] = []
        for path in enabled:
            rot['missions'].append({
                'path': path,   # SDS-relative, backslash, no extension
            })
        sds_save(data)
        flash(f'Mission rotation saved ({len(enabled)} mission(s)). Restart to apply.', 'success')
        return redirect(url_for('missions'))

    missions_list = list_missions()
    active_paths  = [m['path'] for m in rot['missions']]
    rotation_meta = {m['path']: m for m in rot['missions']}

    return render_template('missions.html',
                           missions=missions_list,
                           active_paths=active_paths,
                           rotation_meta=rotation_meta,
                           random_rotation=rot['random'])


@app.route('/missions/upload', methods=['POST'])
@requires_auth
def mission_upload():
    uploaded = request.files.get('mission_file')
    # subfolder is relative to DATA_PATH, e.g. "Cooperative" or "Multiplayer\Dogfight"
    subfolder = request.form.get('folder', 'Dogfight')

    if not uploaded or not uploaded.filename:
        flash('No file selected.', 'danger')
        return redirect(url_for('missions'))

    filename = secure_filename(uploaded.filename)
    if not filename.lower().endswith('.mission'):
        flash('Only .Mission files are allowed.', 'danger')
        return redirect(url_for('missions'))

    target_dir = os.path.join(DATA_PATH, subfolder.replace('\\', os.sep))
    os.makedirs(target_dir, exist_ok=True)
    uploaded.save(os.path.join(target_dir, filename))

    sds_rel = os.path.join(subfolder, os.path.splitext(filename)[0])  # no extension
    flash(f'"{filename}" uploaded. SDS path will be: {sds_rel}', 'success')
    return redirect(url_for('missions'))


@app.route('/missions/upload_companion', methods=['POST'])
@requires_auth
def mission_upload_companion():
    sds_path = request.form.get('sds_path', '')
    uploaded = request.files.get('companion_file')

    if not sds_path or not uploaded or not uploaded.filename:
        flash('Missing mission path or file.', 'danger')
        return redirect(url_for('missions'))

    # Accept missions that have an active .mission OR an active .msnbin
    abs_mission = find_mission_file(sds_path)
    abs_msnbin  = find_msnbin_file(sds_path)
    if abs_mission is None and abs_msnbin is None:
        flash('Mission file not found on disk.', 'danger')
        return redirect(url_for('missions'))

    # Derive stem and target dir from whichever active file exists
    primary = abs_msnbin or abs_mission
    mission_stem = os.path.splitext(os.path.basename(primary))[0]
    target_dir   = os.path.dirname(primary)

    filename = secure_filename(uploaded.filename)
    file_stem, file_ext = os.path.splitext(filename)

    if file_ext.lower() == '.mission':
        flash('Use the main upload form to add .Mission files.', 'danger')
        return redirect(url_for('missions'))

    # secure_filename() replaces spaces with underscores, so normalize both sides
    def _norm(s):
        return s.lower().replace(' ', '_')

    if _norm(file_stem) != _norm(mission_stem):
        flash(
            f'Name mismatch: the uploaded file is named "{file_stem}" but the mission '
            f'is "{mission_stem}". Companion files must have the same base name as the '
            f'.Mission file.',
            'danger',
        )
        return redirect(url_for('missions'))

    if not os.path.realpath(target_dir).startswith(os.path.realpath(DATA_PATH) + os.sep):
        flash('Invalid path — upload refused.', 'danger')
        return redirect(url_for('missions'))

    # Always save using the canonical stem from disk so names stay consistent
    save_filename = mission_stem + file_ext

    # When uploading a .msnbin: move the active .mission to backup so DServer
    # is forced to use the faster binary instead of recompiling on every load.
    if file_ext.lower() == '.msnbin' and abs_mission is not None:
        rel_sub    = os.path.relpath(os.path.dirname(abs_mission), DATA_PATH)
        backup_dir = os.path.join(BACKUP_PATH, rel_sub)
        os.makedirs(backup_dir, exist_ok=True)
        shutil.move(abs_mission, os.path.join(backup_dir, os.path.basename(abs_mission)))

    uploaded.save(os.path.join(target_dir, save_filename))
    extra = ' Original .mission moved to backup.' if file_ext.lower() == '.msnbin' else ''
    flash(f'"{save_filename}" added to mission "{mission_stem}".{extra}', 'success')
    return redirect(url_for('missions'))


@app.route('/missions/remove_msnbin', methods=['POST'])
@requires_auth
def mission_remove_msnbin():
    sds_path   = request.form.get('path', '')
    abs_msnbin = find_msnbin_file(sds_path)
    if abs_msnbin is None:
        flash('No .msnbin file found for this mission.', 'warning')
        return redirect(url_for('missions'))

    if not os.path.realpath(abs_msnbin).startswith(os.path.realpath(DATA_PATH) + os.sep):
        flash('Invalid path — deletion refused.', 'danger')
        return redirect(url_for('missions'))

    os.remove(abs_msnbin)

    # Restore backed-up .mission to its original location if one exists
    abs_backup = find_backup_mission_file(sds_path)
    if abs_backup is not None:
        restore_dir = os.path.join(DATA_PATH, os.path.dirname(sds_path.replace('\\', os.sep)))
        os.makedirs(restore_dir, exist_ok=True)
        shutil.move(abs_backup, os.path.join(restore_dir, os.path.basename(abs_backup)))
        try:
            os.rmdir(os.path.dirname(abs_backup))
        except OSError:
            pass
        flash('.msnbin removed. Original .mission restored.', 'success')
    else:
        flash('.msnbin removed.', 'success')
    return redirect(url_for('missions'))


@app.route('/missions/delete', methods=['POST'])
@requires_auth
def mission_delete():
    sds_path = request.form.get('path', '')

    abs_mission = find_mission_file(sds_path)
    abs_msnbin  = find_msnbin_file(sds_path)
    abs_backup  = find_backup_mission_file(sds_path)

    if abs_mission is None and abs_msnbin is None and abs_backup is None:
        flash('File not found.', 'warning')
        return redirect(url_for('missions'))

    real_data   = os.path.realpath(DATA_PATH)
    real_backup = os.path.realpath(BACKUP_PATH)

    # Remove from SDS rotation if listed, then save
    data = sds_load()
    before = len(data['rotation']['missions'])
    data['rotation']['missions'] = [
        m for m in data['rotation']['missions'] if m['path'] != sds_path
    ]
    if len(data['rotation']['missions']) < before:
        sds_save(data)

    deleted = []

    # Delete all active files and companions in DATA_PATH
    active = abs_mission or abs_msnbin
    if active is not None:
        stem       = os.path.splitext(os.path.basename(active))[0]
        target_dir = os.path.dirname(active)
        for entry in os.scandir(target_dir):
            if not entry.is_file():
                continue
            if os.path.splitext(entry.name)[0].lower() != stem.lower():
                continue
            if not os.path.realpath(entry.path).startswith(real_data + os.sep):
                continue
            os.remove(entry.path)
            deleted.append(entry.name)
    else:
        stem = os.path.splitext(os.path.basename(abs_backup))[0]

    # Delete backed-up .mission if it exists
    if abs_backup is not None:
        if os.path.realpath(abs_backup).startswith(real_backup + os.sep):
            os.remove(abs_backup)
            deleted.append(f'backup/{os.path.basename(abs_backup)}')
            try:
                os.rmdir(os.path.dirname(abs_backup))
            except OSError:
                pass

    flash(f'Deleted {len(deleted)} file(s) for "{stem}": {", ".join(sorted(deleted))}.', 'success')
    return redirect(url_for('missions'))


@app.route('/vnc')
@requires_auth
def vnc():
    host = request.host.split(':')[0]
    return render_template('vnc.html', vnc_host=host, vnc_port=6080)


def find_log_files():
    """
    Return a list of (label, abs_path) for every .log file found in the IL-2
    installation and the Wine user home dir, plus our captured Wine output.
    Most-recently-modified first.
    """
    candidates = []

    # Our own captured Wine/stderr output — always listed first
    if os.path.isfile(LOG_PATH):
        candidates.append(('DServer (wine output)', LOG_PATH))

    # Walk the game directory for .log files; skip Wine internals
    skip = {'drive_c', 'dosdevices', '__pycache__', 'gecko', 'mono'}
    for root, dirs, files in os.walk(IL2_PATH):
        dirs[:] = [d for d in dirs if d not in skip]
        for fname in files:
            if fname.lower().endswith('.log'):
                fpath = os.path.join(root, fname)
                if fpath == LOG_PATH:
                    continue
                label = os.path.relpath(fpath, IL2_PATH)
                candidates.append((label, fpath))

    # Also scan the Wine user home — IL-2 may write logs to C:\Users\root\...
    wine_user_home = os.path.join(WINEPREFIX, 'drive_c', 'users', 'root')
    if os.path.isdir(wine_user_home):
        for root, dirs, files in os.walk(wine_user_home):
            dirs[:] = [d for d in dirs if d not in {'__pycache__'}]
            for fname in files:
                if fname.lower().endswith('.log'):
                    fpath = os.path.join(root, fname)
                    label = 'wine:' + os.path.relpath(fpath, WINEPREFIX)
                    candidates.append((label, fpath))

    def mtime(pair):
        try:
            return os.path.getmtime(pair[1])
        except OSError:
            return 0

    # Keep captured Wine output pinned at top; sort the rest by mtime
    wine_entry = candidates[:1]
    rest       = sorted(candidates[1:], key=mtime, reverse=True)
    return wine_entry + rest


@app.route('/logs')
@requires_auth
def logs():
    log_files   = find_log_files()
    active_file = request.args.get('file', log_files[0][1] if log_files else LOG_PATH)
    return render_template('logs.html', log_files=log_files, active_file=active_file)


@app.route('/logs/stream')
@requires_auth
def logs_stream():
    requested = request.args.get('file', LOG_PATH)

    # Security: must resolve inside IL2_PATH or WINEPREFIX
    real = os.path.realpath(requested)
    allowed = (
        real.startswith(os.path.realpath(IL2_PATH) + os.sep)
        or real.startswith(os.path.realpath(WINEPREFIX) + os.sep)
    )
    stream_path = requested if allowed else LOG_PATH

    def generate():
        try:
            with open(stream_path, 'r', errors='replace') as fh:
                all_lines = fh.readlines()
                if not all_lines:
                    yield 'data: [File exists but is empty — server may write logs elsewhere]\n\n'
                for line in all_lines[-100:]:
                    yield f'data: {line.rstrip()}\n\n'
                while True:
                    line = fh.readline()
                    if line:
                        yield f'data: {line.rstrip()}\n\n'
                    else:
                        time.sleep(0.5)
        except FileNotFoundError:
            yield f'data: [Not found: {stream_path}]\n\n'
            yield 'data: [Start the server and check other files in the dropdown above]\n\n'

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/status')
@requires_auth
def api_status():
    return jsonify({'status': server_status(), 'pid': server_pid()})


# --- Auto-start ----------------------------------------------------------

def _auto_start_worker():
    time.sleep(8)
    ok, msg = do_start()
    print(f'[auto-start] {msg}', flush=True)

if AUTO_START:
    threading.Thread(target=_auto_start_worker, daemon=True).start()

# --- Main ----------------------------------------------------------------

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, threaded=True, debug=False)
