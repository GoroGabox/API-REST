from datetime import datetime

def extract_ids_from_buy_order(buy_order):
    # Suponiendo que buy_order sigue la estructura "order_{course_id}_{student_id}"
    parts = buy_order.split('_')
    if len(parts) == 3:
        _, course_id, student_id = parts
        return int(course_id), int(student_id)
    else:
        # Maneja el caso de error o estructura inesperada
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