"""Usuario que origina un cambio, para registros de auditoría hechos por señales.

Las señales de modelo no conocen el request. La vista envuelve el guardado en
`como_usuario(request.user)` y la señal lee `usuario_actual()`. Vive en el
paquete del proyecto para que cualquier app (incluida `schools`, la de más
bajo nivel) pueda usarlo sin importar `accounts`/`sales`.
"""
from contextlib import contextmanager
from contextvars import ContextVar

_usuario = ContextVar("usuario_auditoria", default=None)


@contextmanager
def como_usuario(user):
    token = _usuario.set(user)
    try:
        yield
    finally:
        _usuario.reset(token)


def usuario_actual():
    user = _usuario.get()
    return user if getattr(user, "is_authenticated", False) else None
