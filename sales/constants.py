# Estados de `Venta.payment_status` que cuentan como pagados. Transbank deja
# 'AUTHORIZED'; flujos internos/tests usan 'approved'.
ESTADOS_PAGADOS = ("AUTHORIZED", "authorized", "approved", "APPROVED", "paid")
