from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.views.decorators.http import require_GET

from .models import Reserva, ConfiguracionReserva, DiaNoLaborable
from .forms import ReservaForm
from .context_helpers import menu_reserva_context
from .services import (
    parse_platos_post,
    guardar_platos_reserva,
    procesar_pago_reserva,
    liberar_mesas_por_no_asistencia,
    liberar_mesa_reserva,
    confirmar_llegada_cliente,
    mesas_disponibles_para,
    parse_fecha_hora_form,
)


def _redirect_despues_reserva(request, reserva):
    if hasattr(request.user, 'perfil_cliente') and reserva.cliente_id == request.user.perfil_cliente.id:
        return redirect('clientes:mis_reservas')
    if not request.user.is_authenticated:
        from django.contrib import messages
        messages.success(request, "¡Operación completada exitosamente!")
        return redirect('clientes:home')
    return redirect('lista_reservas')


@require_GET
def api_mesas_disponibles_reserva(request):
    fecha, hora = parse_fecha_hora_form(
        request.GET.get('fecha'),
        request.GET.get('hora'),
    )
    if not fecha or not hora:
        return JsonResponse({'mesas': []})

    exclude = request.GET.get('exclude')
    exclude_pk = int(exclude) if exclude and exclude.isdigit() else None
    mesas = mesas_disponibles_para(fecha, hora, exclude_pk)

    return JsonResponse({
        'mesas': [
            {
                'id': mesa.id,
                'label': f"Mesa {mesa.numero} ({mesa.zona.nombre}) — {mesa.capacidad} pers.",
            }
            for mesa in mesas
        ],
    })


def pago_reserva(request, pk):
    reserva = get_object_or_404(
        Reserva.objects.prefetch_related('platos__plato'),
        pk=pk,
    )

    if hasattr(request.user, 'perfil_cliente'):
        if reserva.cliente_id != request.user.perfil_cliente.id:
            messages.error(request, 'No puede pagar una reserva que no le pertenece.')
            return redirect('clientes:mis_reservas')

    if reserva.estado == 'CONFIRMADA' and hasattr(reserva, 'pago') and reserva.pago.estado == 'PAGADO':
        messages.info(request, 'Esta reserva ya fue pagada y confirmada.')
        return _redirect_despues_reserva(request, reserva)

    if not reserva.platos.exists():
        messages.error(request, 'Debe seleccionar al menos un plato antes de pagar.')
        return redirect('confirmar_pedido_reserva', pk=pk)

    config = ConfiguracionReserva.objects.first()

    if request.method == 'POST':
        metodo = request.POST.get('metodo_pago')
        try:
            procesar_pago_reserva(
                reserva,
                metodo=metodo,
                referencia=request.POST.get('referencia', ''),
                titular_tarjeta=request.POST.get('titular_tarjeta', ''),
                ultimos_digitos_tarjeta=request.POST.get('ultimos_digitos_tarjeta', ''),
            )
            messages.success(request, 'Pago registrado. Su reserva quedó confirmada.')
            return _redirect_despues_reserva(request, reserva)
        except ValueError as exc:
            messages.error(request, str(exc))

    context = {
        'reserva': reserva,
        'numero_yape': config.numero_yape if config else '987 654 321',
        'numero_plin': config.numero_plin if config else '987 654 321',
        'es_cliente': hasattr(request.user, 'perfil_cliente'),
    }
    return render(request, 'reservas/pago_reserva.html', context)


def _procesar_reserva_con_platos(request, form, reserva_extra=None):
    platos_data = parse_platos_post(request.POST)
    confirmar = request.POST.get('accion') == 'confirmar_pedido'

    if not form.is_valid():
        return None, confirmar

    reserva = form.save(commit=False)
    if reserva_extra:
        for key, value in reserva_extra.items():
            setattr(reserva, key, value)
    reserva.estado = 'PENDIENTE'
    reserva.pedido_confirmado = False
    reserva.save()

    if platos_data:
        guardar_platos_reserva(reserva, platos_data)

    if confirmar:
        if not platos_data:
            return reserva, False
        return reserva, 'pago'

    return reserva, False


@login_required
def lista_reservas(request):
    """Muestra todas las reservas ordenadas"""
    liberar_mesas_por_no_asistencia()

    reservas = Reserva.objects.select_related(
        'mesa', 'mesa__zona', 'cliente', 'comanda',
    ).prefetch_related('platos__plato').order_by('-fecha', '-hora')

    estado = request.GET.get('estado')
    if estado:
        reservas = reservas.filter(estado=estado)

    paginator = Paginator(reservas, 8)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    return render(request, 'reservas/lista.html', {
        'page_obj': page_obj,
        'estados': Reserva.ESTADOS,
    })


@login_required
def crear_reserva(request):
    """Crea una nueva reserva con validaciones"""
    if request.method == 'POST':
        form = ReservaForm(request.POST)
        reserva, resultado = _procesar_reserva_con_platos(
            request, form, {'creado_por': request.user}
        )
        platos_cantidades = {plato_id: qty for plato_id, qty in parse_platos_post(request.POST)}
        if reserva is None:
            messages.error(request, 'Hubo un error con la reserva. Verifica los datos.')
        else:
            accion = request.POST.get('accion')
            if accion == 'confirmar_pedido':
                if not reserva.mesa:
                    messages.error(request, 'Atención: Debe seleccionar una MESA antes de poder confirmar un pedido directo / llegada al restaurante.')
                    # Borramos la reserva recién creada si era un intento de pedido directo fallido para evitar basura
                    reserva.delete()
                    return redirect('lista_reservas')

                reserva.estado = 'CONFIRMADA'
                reserva.pedido_confirmado = True
                reserva.save(update_fields=['estado', 'pedido_confirmado'])
                try:
                    comanda = confirmar_llegada_cliente(reserva, request.user)
                    messages.success(request, f'Mesa ocupada. Comanda enviada a la mesa {reserva.mesa.numero}.')
                except ValueError as exc:
                    messages.error(request, str(exc))
                return redirect('lista_reservas')
            else:
                messages.success(request, 'Reserva guardada como pendiente.')
                return redirect('lista_reservas')
    else:
        form = ReservaForm()
        platos_cantidades = {}

    context = {
        'form': form,
        'titulo': 'Nueva Reserva',
        'platos_cantidades': platos_cantidades,
        **menu_reserva_context(),
    }
    return render(request, 'reservas/crear_modificar.html', context)


def confirmar_pedido_reserva(request, pk):
    reserva = get_object_or_404(
        Reserva.objects.prefetch_related('platos__plato'),
        pk=pk,
    )

    if hasattr(request.user, 'perfil_cliente'):
        if reserva.cliente_id != request.user.perfil_cliente.id:
            messages.error(request, 'No puede confirmar una reserva que no le pertenece.')
            return redirect('clientes:mis_reservas')

    if request.method == 'POST':
        platos_data = parse_platos_post(request.POST)
        if platos_data:
            guardar_platos_reserva(reserva, platos_data)
        return redirect('pago_reserva', pk=pk)

    context = {
        'reserva': reserva,
        'platos_cantidades': {p.plato_id: p.cantidad for p in reserva.platos.all()},
        **menu_reserva_context(),
    }
    return render(request, 'reservas/confirmar_pedido.html', context)


@login_required
def cambiar_estado_reserva(request, pk):
    if request.method == 'POST':
        reserva = get_object_or_404(Reserva, pk=pk)
        nuevo_estado = request.POST.get('estado')
        if nuevo_estado in dict(Reserva.ESTADOS).keys():
            reserva.estado = nuevo_estado
            if nuevo_estado == 'CONFIRMADA':
                reserva.pedido_confirmado = True
            if nuevo_estado in ['CANCELADA', 'NO_ASISTIO', 'FINALIZADA']:
                liberar_mesa_reserva(reserva)
            reserva.save()
            messages.success(request, f'Reserva de {reserva.cliente_nombre} cambiada a {nuevo_estado}.')
        else:
            messages.error(request, 'Estado inválido.')
    return redirect('lista_reservas')


@login_required
def confirmar_llegada_reserva(request, pk):
    if request.method != 'POST':
        return redirect('lista_reservas')

    reserva = get_object_or_404(Reserva, pk=pk)
    try:
        comanda = confirmar_llegada_cliente(reserva, request.user)
        messages.success(
            request,
            f'Llegada confirmada. Comanda {comanda.codigo_comanda} enviada a la mesa {reserva.mesa.numero}.',
        )
    except ValueError as exc:
        messages.error(request, str(exc))

    return redirect('lista_reservas')


@login_required
@require_GET
def api_verificar_no_show(request):
    liberadas = liberar_mesas_por_no_asistencia()
    estados = {
        str(r['id']): r['estado']
        for r in Reserva.objects.values('id', 'estado')
    }
    return JsonResponse({'liberadas': liberadas, 'estados': estados})


@login_required
def panel_configuracion(request):
    """Muestra el panel para modificar horarios y días no laborables (SOLO ADMIN)"""
    if request.user.rol.nombre != 'ADMIN':
        messages.error(request, 'No tienes permisos de administrador.')
        return redirect('index')

    config = ConfiguracionReserva.objects.first()
    dias_bloqueados = DiaNoLaborable.objects.all().order_by('-fecha')

    return render(request, 'reservas/configuracion.html', {
        'config': config,
        'dias_bloqueados': dias_bloqueados,
    })
