"""Image serving routes for QR code and logo."""
import html
import os

import flask_babel
from flask import Response, send_file
from flask_smorest import Blueprint

from pikaraoke.lib.current_app import get_karaoke_instance, get_site_name

_ = flask_babel.gettext

images_bp = Blueprint("images", __name__)


@images_bp.route("/qrcode")
def qrcode():
    """Get QR code image for the web interface URL."""
    k = get_karaoke_instance()
    return send_file(k.qr_code_path, mimetype="image/png")


@images_bp.route("/logo")
def logo():
    """Get the configured logo image.

    When the app is using the bundled default PiKaraoke logo, serve a lightweight
    branded SVG for SmartokePy instead. Custom logos still pass through unchanged.
    """
    k = get_karaoke_instance()
    if os.path.abspath(k.logo_path) == os.path.abspath(k.default_logo_path):
        brand_name = html.escape(get_site_name() or "SmartokePy")
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1600 640" role="img" aria-label="{brand_name}">
  <defs>
    <linearGradient id="smartoke-gradient" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#ff8a00" />
      <stop offset="100%" stop-color="#ff3d00" />
    </linearGradient>
  </defs>
  <rect width="1600" height="640" fill="none" />
  <text x="800" y="300" text-anchor="middle" font-family="Arial, Helvetica, sans-serif" font-size="180" font-weight="700" fill="url(#smartoke-gradient)">{brand_name}</text>
  <text x="800" y="410" text-anchor="middle" font-family="Arial, Helvetica, sans-serif" font-size="66" letter-spacing="22" fill="#ffffff">KARAOKE</text>
</svg>"""
        return Response(svg, mimetype="image/svg+xml")
    return send_file(os.path.abspath(k.logo_path), mimetype="image/png")
