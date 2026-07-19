import logging
from django.db import transaction
from apps.menu.models import Plato, MovimientoPlato

logger = logging.getLogger(__name__)

def descontar_stock_plato(linea_comanda, usuario=None):
    """
    Descuenta automáticamente el stock de un plato cuando la comanda
    cambia a estado servida o completada, evitando descuentos dobles.
    """
    if not linea_comanda.plato.control_stock:
        return
    
    if linea_comanda.stock_descontado:
        return

    with transaction.atomic():
        plato = Plato.objects.select_for_update().get(pk=linea_comanda.plato.pk)

        plato.stock_actual -= linea_comanda.cantidad
        
        # Desactivar disponibilidad si el stock llega a cero
        if plato.stock_actual <= 0:
            plato.disponible = False
            
        plato.save(update_fields=['stock_actual', 'disponible'])

        MovimientoPlato.objects.create(
            plato=plato,
            usuario=usuario,
            tipo='VENTA',
            cantidad=-linea_comanda.cantidad,
            motivo=f'Venta automática (Comanda {linea_comanda.comanda.codigo_comanda})'
        )

        linea_comanda.stock_descontado = True
        linea_comanda.save(update_fields=['stock_descontado'])
        logger.info(f"Descontado stock de plato {plato.nombre} por comanda {linea_comanda.comanda.codigo_comanda}")
