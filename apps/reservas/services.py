from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from apps.mesas.models import Mesa
from apps.menu.models import Plato
from apps.comandas.models import Comanda, LineaComanda
from .models import ConfiguracionReserva, Reserva, ReservaPlato, ReservaPago

TOLERANCIA_DEFAULT = 15

ESTADOS_BLOQUEAN_MESA = ['PENDIENTE', 'CONFIRMADA', 'REPROGRAMADA', 'EN_MESA']

ESTADOS_COMANDA_ACTIVA = [
    Comanda.Estado.ABIERTA,
    Comanda.Estado.EN_PREPARACION,
    Comanda.Estado.LISTA,
]


def _intervalo_reserva_minutos():
    config = ConfiguracionReserva.objects.first()
    return config.intervalo_minutos if config else 60


def reservas_se_solapan(fecha1, hora1, fecha2, hora2, intervalo_minutos=None):
    if fecha1 != fecha2:
        return False
    intervalo = intervalo_minutos or _intervalo_reserva_minutos()
    dt1 = datetime.combine(fecha1, hora1)
    dt2 = datetime.combine(fecha2, hora2)
    delta = timedelta(minutes=intervalo)
    return (dt1 - delta) < (dt2 + delta) and (dt2 - delta) < (dt1 + delta)


def mesa_ocupada_en_horario(mesa_id, fecha, hora, exclude_reserva_id=None):
    intervalo = _intervalo_reserva_minutos()
    reservas = Reserva.objects.filter(
        mesa_id=mesa_id,
        fecha=fecha,
        estado__in=ESTADOS_BLOQUEAN_MESA,
    )
    if exclude_reserva_id:
        reservas = reservas.exclude(pk=exclude_reserva_id)

    for reserva in reservas:
        if reservas_se_solapan(fecha, hora, reserva.fecha, reserva.hora, intervalo):
            return True
    return False


def mesas_disponibles_para(fecha, hora, exclude_reserva_id=None):
    mesas = Mesa.objects.filter(activo=True).select_related('zona').order_by('zona__nombre', 'numero')
    if not fecha or not hora:
        return mesas.none()

    ids_disponibles = [
        mesa.pk for mesa in mesas
        if not mesa_ocupada_en_horario(mesa.pk, fecha, hora, exclude_reserva_id)
    ]
    return mesas.filter(pk__in=ids_disponibles)


def obtener_reserva_prepagada_comanda(comanda):
    """Obtiene la reserva con pago online vinculada a una comanda."""
    reserva = (
        Reserva.objects.filter(comanda=comanda)
        .select_related('pago', 'mesa', 'mesa__zona')
        .first()
    )
    if reserva and hasattr(reserva, 'pago') and reserva.pago.estado == 'PAGADO':
        return reserva

    return (
        Reserva.objects.filter(
            mesa=comanda.mesa,
            pago__estado='PAGADO',
            pedido_confirmado=True,
        )
        .select_related('pago', 'mesa', 'mesa__zona')
        .order_by('-fecha', '-hora')
        .first()
    )


def parse_fecha_hora_form(fecha_val, hora_val):
    if not fecha_val or not hora_val:
        return None, None
    try:
        if hasattr(fecha_val, 'year'):
            fecha = fecha_val
        else:
            fecha = datetime.strptime(str(fecha_val), '%Y-%m-%d').date()
        if hasattr(hora_val, 'hour'):
            hora = hora_val
        else:
            hora_str = str(hora_val)
            hora = datetime.strptime(hora_str[:5], '%H:%M').time()
        return fecha, hora
    except (ValueError, TypeError):
        return None, None


def parse_platos_post(post_data):
    platos = []
    for key, value in post_data.items():
        if not key.startswith('plato_'):
            continue
        try:
            plato_id = int(key.replace('plato_', ''))
            cantidad = int(value)
        except (ValueError, TypeError):
            continue
        if cantidad > 0:
            platos.append((plato_id, cantidad))
    return platos


def guardar_platos_reserva(reserva, platos_data):
    ReservaPlato.objects.filter(reserva=reserva).delete()
    for plato_id, cantidad in platos_data:
        plato = Plato.objects.get(pk=plato_id, activo=True, disponible=True)
        ReservaPlato.objects.create(
            reserva=reserva,
            plato=plato,
            cantidad=cantidad,
            precio_unitario=plato.precio_actual,
        )


def confirmar_pedido_reserva(reserva):

    if not hasattr(reserva, 'pago') or reserva.pago.estado != 'PAGADO':
        raise ValueError('Debe completar el pago antes de confirmar la reserva.')

    reserva.pedido_confirmado = True
    reserva.estado = 'CONFIRMADA'
    reserva.save(update_fields=['pedido_confirmado', 'estado', 'fecha_actualizacion'])

    if reserva.mesa_id:
        Mesa.objects.filter(pk=reserva.mesa_id).update(estado=Mesa.Estado.RESERVADA)


def procesar_pago_reserva(reserva, metodo, referencia='', titular_tarjeta='', ultimos_digitos_tarjeta=''):

    if metodo not in dict(ReservaPago.METODOS):
        raise ValueError('Método de pago no válido.')

    if metodo in ('YAPE', 'PLIN') and not referencia.strip():
        raise ValueError('Ingrese el número de operación del pago.')

    if metodo == 'TARJETA':
        if not titular_tarjeta.strip():
            raise ValueError('Ingrese el titular de la tarjeta.')
        if len(ultimos_digitos_tarjeta.strip()) != 4 or not ultimos_digitos_tarjeta.strip().isdigit():
            raise ValueError('Ingrese los últimos 4 dígitos válidos de la tarjeta.')

    pago, _ = ReservaPago.objects.update_or_create(
        reserva=reserva,
        defaults={
            'metodo': metodo,
            'monto': reserva.total_pedido,
            'estado': 'PAGADO',
            'referencia': referencia.strip() or None,
            'titular_tarjeta': titular_tarjeta.strip() or None,
            'ultimos_digitos_tarjeta': ultimos_digitos_tarjeta.strip() or None,
            'fecha_pago': timezone.now(),
        },
    )

    confirmar_pedido_reserva(reserva)
    return pago


def liberar_mesa_reserva(reserva):
    if not reserva.mesa_id:
        return

    mesa_id = reserva.mesa_id
    otras_activas = Reserva.objects.filter(
        mesa_id=mesa_id,
        estado__in=['PENDIENTE', 'CONFIRMADA', 'REPROGRAMADA'],
    ).exclude(pk=reserva.pk).exists()

    if not otras_activas:
        Mesa.objects.filter(pk=mesa_id, estado=Mesa.Estado.RESERVADA).update(estado=Mesa.Estado.LIBRE)


def _reserva_expiro_tolerancia(reserva, tolerancia, ahora=None):
    ahora = ahora or timezone.localtime()
    limite = timezone.make_aware(datetime.combine(reserva.fecha, reserva.hora)) + timedelta(minutes=tolerancia)
    return ahora >= limite


def liberar_mesas_por_no_asistencia():
    config = ConfiguracionReserva.objects.first()
    tolerancia = config.tolerancia_no_show_minutos if config else TOLERANCIA_DEFAULT
    ahora = timezone.localtime()

    candidatas = Reserva.objects.filter(
        estado='CONFIRMADA',
        pedido_confirmado=True,
        cliente_llego=False,
    ).select_related('mesa')

    liberadas = 0
    for reserva in candidatas:
        if not _reserva_expiro_tolerancia(reserva, tolerancia, ahora):
            continue

        reserva.estado = 'NO_ASISTIO'
        reserva.save(update_fields=['estado', 'fecha_actualizacion'])
        liberar_mesa_reserva(reserva)
        liberadas += 1

    return liberadas


def obtener_comanda_activa_mesa(mesa):
    comanda = (
        Comanda.objects.filter(mesa=mesa, estado__in=ESTADOS_COMANDA_ACTIVA)
        .order_by('-fecha_apertura')
        .first()
    )
    if comanda:
        return comanda

    reserva = (
        Reserva.objects.filter(mesa=mesa, estado='EN_MESA', comanda__isnull=False)
        .select_related('comanda')
        .order_by('-fecha', '-hora')
        .first()
    )
    if reserva and reserva.comanda and reserva.comanda.estado in ESTADOS_COMANDA_ACTIVA:
        return reserva.comanda
    return None


def serializar_comanda_desde_reserva(reserva):
    lineas = []
    total = 0
    for item in reserva.platos.select_related('plato'):
        lineas.append({
            'id': item.pk,
            'plato_id': item.plato_id,
            'plato_nombre': item.plato.nombre,
            'cantidad': item.cantidad,
            'precio_unitario': str(item.precio_unitario),
            'subtotal': str(item.subtotal),
            'estado': 'PENDIENTE',
            'estado_label': 'Reserva anticipada',
            'notas_cocina': '',
        })
        total += item.subtotal

    prepago = hasattr(reserva, 'pago') and reserva.pago.estado == 'PAGADO'
    return {
        'id': None,
        'reserva_id': reserva.id,
        'es_reserva_pendiente': True,
        'prepago_reserva': prepago,
        'fecha_apertura': 'Reserva confirmada',
        'mesero': 'Reserva online',
        'nombre_cliente': reserva.cliente_nombre,
        'notas': reserva.observaciones or '',
        'total': str(total),
        'lineas': lineas,
    }


def asegurar_comanda_desde_reserva(mesa, usuario):
    comanda = obtener_comanda_activa_mesa(mesa)
    if comanda:
        return comanda

    reserva = (
        Reserva.objects.filter(
            mesa=mesa,
            estado='CONFIRMADA',
            pedido_confirmado=True,
            cliente_llego=False,
        )
        .prefetch_related('platos__plato')
        .order_by('-fecha', '-hora')
        .first()
    )
    if not reserva or not reserva.platos.exists():
        return None

    if not usuario or not getattr(usuario, 'is_authenticated', False):
        raise ValueError('Debe iniciar sesión para confirmar la llegada del cliente.')

    return confirmar_llegada_cliente(reserva, usuario)


def confirmar_llegada_cliente(reserva, usuario):
    if reserva.estado != 'CONFIRMADA':
        raise ValueError('Solo se puede confirmar llegada en reservas confirmadas.')
    if reserva.cliente_llego:
        raise ValueError('La llegada de este cliente ya fue confirmada.')
    if not reserva.mesa_id:
        raise ValueError('Asigne una mesa antes de confirmar la llegada.')

    mesa = reserva.mesa
    if mesa.estado not in (Mesa.Estado.LIBRE, Mesa.Estado.RESERVADA):
        raise ValueError(f'La mesa {mesa.numero} no está disponible ({mesa.get_estado_display()}).')

    otra_comanda = Comanda.objects.filter(
        mesa=mesa,
        estado__in=ESTADOS_COMANDA_ACTIVA,
    ).exists()
    if otra_comanda:
        raise ValueError(f'La mesa {mesa.numero} ya tiene una comanda activa.')

    with transaction.atomic():
        hoy = timezone.localdate()
        count = Comanda.objects.filter(fecha_apertura__date=hoy).count() + 1
        codigo = f"COM-{hoy.strftime('%Y%m%d')}-{count:03d}"

        comanda = Comanda.objects.create(
            codigo_comanda=codigo,
            mesa=mesa,
            mozo=usuario,
            nombre_cliente=reserva.cliente_nombre[:100],
            estado=Comanda.Estado.ABIERTA,
            observacion_general=reserva.observaciones or f'Pedido de reserva #RES-{reserva.id:04d}',
        )

        ahora_linea = timezone.now()
        for item in reserva.platos.select_related('plato'):
            LineaComanda.objects.create(
                comanda=comanda,
                plato=item.plato,
                cantidad=item.cantidad,
                precio_unitario=item.precio_unitario,
                subtotal=item.subtotal,
                observacion=reserva.observaciones or 'Reserva anticipada',
                fecha_envio_cocina=ahora_linea,
                tiempo_estimado_min=item.plato.tiempo_preparacion_min or 0,
                estado=LineaComanda.Estado.PENDIENTE,
            )

        comanda.calcular_totales()

        mesa.estado = Mesa.Estado.OCUPADA
        mesa.save(update_fields=['estado'])

        reserva.cliente_llego = True
        reserva.fecha_llegada = ahora_linea
        reserva.comanda = comanda
        reserva.estado = 'EN_MESA'
        reserva.save(update_fields=[
            'cliente_llego', 'fecha_llegada', 'comanda', 'estado', 'fecha_actualizacion',
        ])

    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            'kds_updates',
            {
                'type': 'kds_update',
                'action': 'nueva_comanda',
                'detail': {'comanda_id': comanda.id, 'mesa': mesa.numero},
            },
        )
    except Exception:
        pass

    return comanda

def notificar_nueva_reserva_admin(reserva_id):
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            'reservas_admin_updates',
            {
                'type': 'reserva_update',
                'action': 'nueva_reserva',
                'detail': {'reserva_id': reserva_id},
            },
        )
    except Exception:
        pass
