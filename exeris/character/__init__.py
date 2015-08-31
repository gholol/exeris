import types
from flask import Blueprint
from exeris.app import with_sijax_route

character_bp = Blueprint('character',
                         __name__,
                         template_folder='templates',
                         static_folder="static", static_url_path="/static/character",
                         url_prefix="/character/<character_id>")

# monkey patching to make the decorator more comfortable to use
character_bp.with_sijax_route = types.MethodType(with_sijax_route, character_bp)

from exeris.character import views
