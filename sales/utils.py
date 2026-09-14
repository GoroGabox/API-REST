from datetime import datetime

def extract_ids_from_buy_order(buy_order):
    # buy_order: "order_{item_id}_{student_id}" (producto) o, para compra
    # individual de curso, "order_{curso_id}_{student_id}_{dias}" (4 segmentos).
    if not isinstance(buy_order, str):
        return None, None
    parts = buy_order.split('_')
    if len(parts) not in (3, 4):
        return None, None
    try:
        return int(parts[1]), int(parts[2])
    except (TypeError, ValueError):
        # Ids no numéricos → formato inválido, no un 500.
        return None, None


def extract_dias_from_buy_order(buy_order):
    """Días del plan si el buy_order de curso los codifica (4º segmento).

    Devuelve None para el formato legacy de 3 segmentos (se asume 7 días).
    """
    if not isinstance(buy_order, str):
        return None
    parts = buy_order.split('_')
    if len(parts) != 4:
        return None
    try:
        return int(parts[3])
    except (TypeError, ValueError):
        return None

def parse_accounting_date(accounting_date):
    try:
        # Agregar el año actual para completar el formato YYYY-MM-DD
        year = datetime.now().year
        month = int(accounting_date[:2])
        day = int(accounting_date[2:])
        return datetime(year, month, day)
    except (ValueError, TypeError):
        return None  # Retorna None si el formato es inválido