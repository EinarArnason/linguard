import http
import traceback
from datetime import datetime, timedelta
from http.client import BAD_REQUEST, NOT_FOUND, INTERNAL_SERVER_ERROR, UNAUTHORIZED, NO_CONTENT
from logging import warning, debug, error, info

from flask import Blueprint, abort, request, Response, redirect, url_for
from flask_login import current_user, login_required, login_user

from core.app_manager import manager
from core.config.linguard_config import config as linguard_config
from core.config.logger_config import config as logger_config
from core.config.web_config import config as web_config
from core.exceptions import WireguardError
from core.models import interfaces
from web.controllers.RestController import RestController
from web.controllers.ViewController import ViewController
from web.models import users
from web.static.assets.resources import EMPTY_FIELD, APP_NAME
from web.utils import get_all_interfaces, get_routing_table, get_wg_interfaces_summary, get_wg_interface_status, \
    get_network_adapters


class Router(Blueprint):

    def __init__(self, name, import_name):
        super().__init__(name, import_name)
        self.login_attempts = 1
        self.banned_until = None


router = Router("router", __name__)


@router.route("/")
@router.route("/dashboard")
@login_required
def index():
    context = {
        "title": "Dashboard"
    }
    return ViewController("web/index.html", **context).load()


@router.route("/logout")
@login_required
def logout():
    current_user.logout()
    return redirect(url_for("router.index"))


@router.route("/signup", methods=["GET"])
def signup():
    if len(users) > 0:
        return redirect(url_for("router.index"))
    from web.forms import SignupForm
    context = {
        "title": "Create admin account",
        "form": SignupForm()
    }
    return ViewController("web/signup.html", **context).load()


@router.route("/signup", methods=["POST"])
def signup_post():
    if len(users) > 0:
        abort(http.HTTPStatus.UNAUTHORIZED)
    from web.forms import SignupForm
    form = SignupForm(request.form)
    return RestController().signup(form)


@router.route("/login", methods=["GET"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("router.index"))
    if len(users) < 1:
        return redirect(url_for("router.signup", next=request.args.get("next", None)))
    from web.forms import LoginForm
    context = {
        "title": "Login",
        "form": LoginForm()
    }
    now = datetime.now()
    if router.banned_until and now < router.banned_until:
        context["banned_for"] = (router.banned_until - now).seconds
    else:
        router.banned_until = None
        router.login_attempts = 1
    return ViewController("web/login.html", **context).load()


@router.route("/login", methods=["POST"])
def login_post():
    from web.forms import LoginForm
    form = LoginForm(request.form)
    info(f"Logging in user '{form.username.data}'...")
    max_attempts = int(web_config.login_attempts)
    if max_attempts and router.login_attempts > max_attempts:
        router.banned_until = datetime.now() + timedelta(minutes=2)
        return redirect(form.next.data or url_for("router.index"))
    router.login_attempts += 1
    if not form.validate():
        context = {
            "title": "Login",
            "form": form
        }
        return ViewController("web/login.html", **context).load()
    u = users.get_by_name(form.username.data)
    if not login_user(u, form.remember_me.data):
        error(f"Unable to log user in.")
        abort(http.HTTPStatus.INTERNAL_SERVER_ERROR)
    info(f"Successfully logged user '{u.name}' in!")
    router.web_login_attempts = 1
    return redirect(form.next.data or url_for("router.index"))


@router.route("/network")
@login_required
def network():
    wg_ifaces = list(interfaces.values())
    ifaces = get_all_interfaces(wg_bin=linguard_config.wg_bin, wg_interfaces=wg_ifaces)
    routes = get_routing_table()
    context = {
        "title": "Network",
        "interfaces": ifaces,
        "routes": routes,
        "last_update": datetime.now().strftime("%H:%M"),
        "EMPTY_FIELD": EMPTY_FIELD
    }
    return ViewController("web/network.html", **context).load()


@router.route("/wireguard")
@login_required
def wireguard():
    wg_ifaces = list(interfaces.values())
    ifaces = get_wg_interfaces_summary(wg_bin=linguard_config.wg_bin, interfaces=wg_ifaces)
    context = {
        "title": "Wireguard",
        "interfaces": ifaces,
        "last_update": datetime.now().strftime("%H:%M"),
        "EMPTY_FIELD": EMPTY_FIELD
    }
    return ViewController("web/wireguard.html", **context).load()


@router.route("/wireguard/interfaces/add", methods=['GET'])
@login_required
def create_wireguard_iface():
    iface = manager.generate_interface()
    context = {
        "title": "Add interface",
        "iface": iface,
        "EMPTY_FIELD": EMPTY_FIELD,
        "APP_NAME": APP_NAME
    }
    return ViewController("web/wireguard-add-iface.html", **context).load()


@router.route("/wireguard/interfaces/add/<uuid>", methods=['POST'])
@login_required
def add_wireguard_iface(uuid: str):
    data = request.json["data"]
    return RestController(uuid).add_iface(data)


@router.route("/wireguard/interfaces/<uuid>", methods=['GET'])
@login_required
def get_wireguard_iface(uuid: str):
    if uuid not in interfaces:
        abort(NOT_FOUND, f"Unknown interface '{uuid}'.")
    iface = interfaces[uuid]
    iface_status = get_wg_interface_status(linguard_config.wg_bin, iface.name)
    context = {
        "title": "Edit interface",
        "iface": iface,
        "iface_status": iface_status,
        "last_update": datetime.now().strftime("%H:%M"),
        "EMPTY_FIELD": EMPTY_FIELD,
        "APP_NAME": APP_NAME
    }
    return ViewController("web/wireguard-iface.html", **context).load()


@router.route("/wireguard/interfaces/<uuid>/save", methods=['POST'])
@login_required
def save_wireguard_iface(uuid: str):
    if uuid not in interfaces:
        abort(NOT_FOUND, f"Interface {uuid} not found.")
    data = request.json["data"]
    return RestController(uuid).apply_iface(data)


@router.route("/wireguard/interfaces/<uuid>/remove", methods=['DELETE'])
@login_required
def remove_wireguard_iface(uuid: str):
    if uuid not in interfaces:
        abort(NOT_FOUND, f"Interface {uuid} not found.")
    return RestController(uuid).remove_iface()


@router.route("/wireguard/interfaces/<uuid>/regenerate-keys", methods=['POST'])
@login_required
def regenerate_iface_keys(uuid: str):
    return RestController(uuid).regenerate_iface_keys()


@router.route("/wireguard/interfaces/<uuid>", methods=['POST'])
@login_required
def operate_wireguard_iface(uuid: str):
    action = request.json["action"].lower()
    try:
        if action == "start":
            manager.iface_up(uuid)
            return Response(status=NO_CONTENT)
        if action == "restart":
            manager.restart_iface(uuid)
            return Response(status=NO_CONTENT)
        if action == "stop":
            manager.iface_down(uuid)
            return Response(status=NO_CONTENT)
        raise WireguardError(f"Invalid operation: {action}", BAD_REQUEST)
    except WireguardError as e:
        return Response(e.cause, status=e.http_code)


@router.route("/wireguard/interfaces", methods=['POST'])
@login_required
def operate_wireguard_ifaces():
    action = request.json["action"].lower()
    try:
        if action == "start":
            for iface in interfaces.values():
                manager.iface_up(iface.uuid)
            return Response(status=NO_CONTENT)
        if action == "restart":
            for iface in interfaces.values():
                manager.restart_iface(iface.uuid)
            return Response(status=NO_CONTENT)
        if action == "stop":
            for iface in interfaces.values():
                manager.iface_down(iface.uuid)
            return Response(status=NO_CONTENT)
        raise WireguardError(f"invalid operation: {action}", BAD_REQUEST)
    except WireguardError as e:
        return Response(e.cause, status=e.http_code)


@router.route("/wireguard/peers/add", methods=['GET'])
@login_required
def create_wireguard_peer():
    iface = None
    iface_uuid = request.args.get("interface")
    if iface_uuid:
        if iface_uuid not in interfaces:
            abort(BAD_REQUEST, f"Unable to create peer for unknown interface '{iface_uuid}'.")
        iface = interfaces[iface_uuid]
    peer = manager.generate_peer(iface)
    ifaces = get_wg_interfaces_summary(wg_bin=linguard_config.wg_bin,
                                           interfaces=list(interfaces.values())).values()
    context = {
        "title": "Add peer",
        "peer": peer,
        "interfaces": ifaces,
        "EMPTY_FIELD": EMPTY_FIELD,
        "APP_NAME": APP_NAME
    }
    return ViewController("web/wireguard-add-peer.html", **context).load()


@router.route("/wireguard/peers/add", methods=['POST'])
@login_required
def add_wireguard_peer():
    data = request.json["data"]
    return RestController().add_peer(data)


@router.route("/wireguard/peers/<uuid>/remove", methods=['DELETE'])
@login_required
def remove_wireguard_peer(uuid: str):
    return RestController().remove_peer(uuid)


@router.route("/wireguard/peers/<uuid>", methods=['GET'])
@login_required
def get_wireguard_peer(uuid: str):
    peer = None
    for iface in interfaces.values():
        if uuid in iface.peers:
            peer = iface.peers[uuid]
    if not peer:
        abort(NOT_FOUND, f"Unknown peer '{uuid}'.")
    context = {
        "title": "Edit peer",
        "peer": peer,
        "last_update": datetime.now().strftime("%H:%M"),
        "EMPTY_FIELD": EMPTY_FIELD,
        "APP_NAME": APP_NAME
    }
    return ViewController("web/wireguard-peer.html", **context).load()


@router.route("/wireguard/peers/<uuid>/save", methods=['POST'])
@login_required
def save_wireguard_peers(uuid: str):
    data = request.json["data"]
    return RestController(uuid).save_peer(data)


@router.route("/wireguard/peers/<uuid>/download", methods=['GET'])
@login_required
def download_wireguard_peer(uuid: str):
    return RestController(uuid).download_peer()


@router.route("/themes")
@login_required
def themes():
    context = {
        "title": "Themes"
    }
    return ViewController("web/themes.html", **context).load()


@router.route("/settings")
@login_required
def settings():
    from web.forms import SettingsForm
    form = SettingsForm()
    context = {
        "title": "Settings",
        "form": form
    }
    return ViewController("web/settings.html", **context).load()


@router.route("/settings", methods=['POST'])
@login_required
def save_settings():
    from web.forms import SettingsForm
    form = SettingsForm(request.form)
    context = {
        "title": "Settings",
        "form": form
    }
    if form.validate():
        try:
            RestController().save_settings(form)
            # Fill fields with default values if they were left unfilled
            form.log_file.data = form.log_file.data or logger_config.logfile

            ifaces = []
            for k, v in get_network_adapters().items():
                ifaces.append((k, v))
            form.web_adapter.data = form.web_adapter.data or ifaces[web_config.host]
            form.web_secret_key.data = form.web_secret_key.data or web_config.secret_key
            form.web_credentials_file.data = form.web_credentials_file.data or web_config.credentials_file

            form.app_endpoint.data = form.app_endpoint.data or linguard_config.endpoint
            form.app_wg_bin.data = form.app_wg_bin.data or linguard_config.wg_bin
            form.app_wg_quick_bin.data = form.app_wg_quick_bin.data or linguard_config.wg_quick_bin
            form.app_iptables_bin.data = form.app_iptables_bin.data or linguard_config.iptables_bin
            form.app_interfaces_folder.data = form.app_interfaces_folder.data or linguard_config.interfaces_folder

            context["success"] = True
        except Exception as e:
            error(f"{traceback.format_exc()}")
            context["error"] = True
            context["error_details"] = e
    return ViewController("web/settings.html", **context).load()


@router.app_errorhandler(BAD_REQUEST)
def bad_request(err):
    error_code = 400
    context = {
        "title": error_code,
        "error_code": error_code,
        "error_msg": str(err).split(":", 1)[1]
    }
    return ViewController("error/error-main.html", **context).load(), error_code


@router.app_errorhandler(UNAUTHORIZED)
def unauthorized(err):
    warning(f"Unauthorized request from {request.remote_addr}!")
    if request.method == "GET":
        debug(f"Redirecting to login...")
        try:
            next = url_for(request.endpoint)
        except Exception:
            uuid = request.path.rsplit("/", 1)[-1]
            next = url_for(request.endpoint, uuid=uuid)
        return redirect(url_for("router.login", next=next))
    error_code = int(http.HTTPStatus.UNAUTHORIZED)
    context = {
        "title": error_code,
        "error_code": error_code,
        "error_msg": str(err).split(":", 1)[1]
    }
    return ViewController("error/error-main.html", **context).load(), error_code


@router.app_errorhandler(NOT_FOUND)
def not_found(err):
    error_code = int(http.HTTPStatus.NOT_FOUND)
    context = {
        "title": error_code,
        "error_code": error_code,
        "error_msg": str(err).split(":", 1)[1],
        "image": "/static/assets/img/error-404-monochrome.svg"
    }
    return ViewController("error/error-img.html", **context).load(), error_code


@router.app_errorhandler(INTERNAL_SERVER_ERROR)
def not_found(err):
    error_code = int(http.HTTPStatus.INTERNAL_SERVER_ERROR)
    context = {
        "title": error_code,
        "error_code": error_code,
        "error_msg": str(err).split(":", 1)[1]
    }
    return ViewController("error/error-main.html", **context).load(), error_code
