from datetime import datetime

def extract_ids_from_buy_order(buy_order):
    # buy_order sigue la estructura "order_{product_id}_{student_id}".
    if not isinstance(buy_order, str):
        return None, None
    parts = buy_order.split('_')
    if len(parts) != 3:
        return None, None
    _, first_id, student_id = parts
    try:
        return int(first_id), int(student_id)
    except (TypeError, ValueError):
        # Ids no numéricos → formato inválido, no un 500.
        return None, None

def parse_accounting_date(accounting_date):
    try:
        # Agregar el año actual para completar el formato YYYY-MM-DD
        year = datetime.now().year
        month = int(accounting_date[:2])
        day = int(accounting_date[2:])
        return datetime(year, month, day)
    except (ValueError, TypeError):
        return None  # Retorna None si el formato es inválido