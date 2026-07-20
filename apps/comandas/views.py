"""
Vistas y API endpoints para la app comandas.

API Endpoints:
  POST  /api/comandas/crear/           → Crea Comanda + LineaComanda atómicamente
  PATCH /api/comandas/linea/<id>/editar/ → Edita una LineaComanda (ej. cancelada)
  POST  /api/mesas/<id>/liberar/       → Cierra comanda y libera la mesa
"""
from __future__ import annotations
import json
import logging
from django.http import JsonResponse

logger = logging.getLogger(__name__)
from django.views.decorators.http import require_POST, require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.db import transaction
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from apps.usuarios.decorators import rol_requerido
from apps.usuarios.permissions import EsMozoOAdmin, EsCocineroOAdmin, EsAdmin

from .models import Comanda, LineaComanda
from apps.mesas.models import Mesa
from apps.menu.models import Plato
from apps.inventario.models import RecetaInsumo
from apps.reservas.services import obtener_comanda_activa_mesa, asegurar_comanda_desde_reserva

def verificar_stock_plato(plato, cantidad_pedida):
    """
    Verifica si hay stock suficiente de todos los insumos de un plato.
    Retorna (True, None) o (False, error_data).
    """
    recetas = RecetaInsumo.objects.filter(plato=plato, activo=True).select_related('insumo')
    for receta in recetas:
        stock_requerido = receta.cantidad_por_porcion * cantidad_pedida
        if receta.insumo.stock_real < stock_requerido:
            return False, {
                "error": "Stock insuficiente",
                "insumo": receta.insumo.nombre,
                "stock_disponible": float(receta.insumo.stock_real),
                "stock_requerido": float(stock_requerido)
            }
    return True, None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_json_body(request) -> tuple[dict | list, None] | tuple[None, JsonResponse]:
    """Parsea el body JSON de la request. Devuelve (data, None) o (None, error_response)."""
    try:
        data = json.loads(request.body)
        return data, None
    except (json.JSONDecodeError, ValueError):
        return None, JsonResponse({'ok': False, 'error': 'JSON inválido'}, status=400)


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/comandas/crear/
# ─────────────────────────────────────────────────────────────────────────────

@csrf_exempt
@api_view(['POST'])
@permission_classes([EsMozoOAdmin])
def api_crear_comanda(request):
    """
    Crea una Comanda con sus LineaComanda de forma atómica.

    Body JSON esperado desde Alpine.js:
    {
      "mesa_id": 3,
      "notas": "Sin sal en la sopa",
      "items": [
        { "plato_id": 12, "cantidad": 2, "notas": "Bien cocido" },
        { "plato_id":  7, "cantidad": 1, "notas": "" }
      ]
    }

    Respuesta exitosa:
    { "ok": true, "comanda_id": 42, "redirect": "/mesero/mesas/" }
    """
    data = request.data

    # ── Validaciones básicas ──────────────────────────────────────────────────
    mesa_ids = data.get('mesa_ids', [])
    # Compatibilidad con legacy (solo un mesa_id)
    if not mesa_ids and data.get('mesa_id'):
        mesa_ids = [data.get('mesa_id')]
    
    items = data.get('items', [])

    if not mesa_ids:
        return JsonResponse({'ok': False, 'error': 'Falta el campo mesa_ids'}, status=400)
    if len(mesa_ids) > 3:
        return JsonResponse({'ok': False, 'error': 'Máximo 3 mesas permitidas'}, status=400)
    if not items:
        return JsonResponse({'ok': False, 'error': 'El pedido no tiene ítems'}, status=400)

    # ── Obtener las mesas y verificar que estén libres ────────────────────────────
    mesas = Mesa.objects.filter(pk__in=mesa_ids)
    if mesas.count() != len(mesa_ids):
        return JsonResponse({'ok': False, 'error': 'Una o más mesas no encontradas'}, status=404)

    from django.utils import timezone
    import datetime
    from apps.reservas.models import Reserva

    ahora_dt = timezone.localtime()
    limite_dt = ahora_dt + datetime.timedelta(minutes=40)
    umbral_pasado = ahora_dt - datetime.timedelta(hours=2)

    for m in mesas:
        if m.estado != Mesa.Estado.LIBRE:
            return JsonResponse(
                {'ok': False, 'error': f'La mesa {m.numero} no está libre (estado: {m.get_estado_display()})'},
                status=409
            )
        
        conflictos = Reserva.objects.filter(mesa=m, estado__in=['PENDIENTE', 'CONFIRMADA'])
        for r in conflictos:
            if r.fecha and r.hora:
                dt_res = timezone.make_aware(datetime.datetime.combine(r.fecha, r.hora))
                if umbral_pasado <= dt_res <= limite_dt:
                    return JsonResponse(
                        {'ok': False, 'error': f'La mesa {m.numero} está reservada (40 min de bloqueo antes de reserva).'},
                        status=409
                    )

    # ── Bloque atómico: todo o nada ───────────────────────────────────────────
    try:
        with transaction.atomic():
            # 1. Crear la Comanda (usamos la primera mesa como principal)
            mesa_principal = mesas[0]
            mesas_adicionales = mesas[1:]

            import datetime
            now = datetime.datetime.now()
            count = Comanda.objects.filter(fecha_apertura__date=now.date()).count() + 1
            codigo = f"COM-{now.strftime('%Y%m%d')}-{count:03d}"

            comanda = Comanda.objects.create(
                codigo_comanda = codigo,
                mesa   = mesa_principal,
                mozo   = request.user if request.user.is_authenticated else None,
                nombre_cliente = data.get('nombre_cliente', ''),
                estado = Comanda.Estado.ABIERTA,
                observacion_general = data.get('notas', ''),
            )
            
            if mesas_adicionales:
                comanda.mesas_adicionales.set(mesas_adicionales)

            # 2. Crear las LineaComanda
            ahora_linea = timezone.now()
            for item in items:
                plato_id = item.get('plato_id')
                cantidad = int(item.get('cantidad', 1))

                if not plato_id or cantidad < 1:
                    raise ValueError(f'Ítem inválido: {item}')
                
                plato = Plato.objects.get(pk=plato_id)
                
                # --- VERIFICACIÓN DE STOCK ---
                apto, error_data = verificar_stock_plato(plato, cantidad)
                if not apto:
                    return JsonResponse(error_data, status=400)

                LineaComanda.objects.create(
                    comanda         = comanda,
                    plato           = plato,
                    cantidad        = cantidad,
                    precio_unitario = plato.precio_actual,
                    subtotal        = plato.precio_actual * cantidad,
                    observacion     = item.get('notas', ''),
                    fecha_envio_cocina = ahora_linea,
                    tiempo_estimado_min = plato.tiempo_preparacion_min or 0,
                    estado = LineaComanda.Estado.EN_PREP,
                    fecha_inicio_prep = ahora_linea,
                )

            # 3. Marcar TODAS las mesas como OCUPADA
            for m in mesas:
                m.estado = Mesa.Estado.OCUPADA
                m.save(update_fields=['estado'])

            # 4. Calcular totales
            comanda.calcular_totales()

    except Plato.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Un plato indicado no existe'}, status=404)
    except ValueError as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=400)
    except Exception as e:
        return JsonResponse({'ok': False, 'error': f'Error interno: {str(e)}'}, status=500)

    # 5. Notificar al KDS vía WebSocket para actualización inmediata
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            'kds_updates',
            {
                'type': 'kds_update',
                'action': 'nueva_comanda',
                'detail': {'comanda_id': comanda.id, 'mesa': mesa_principal.numero},
            }
        )
    except Exception:
        pass  # El WS es opcional, el polling es el fallback

    return JsonResponse({
        'ok': True, 
        'comanda_id': comanda.id, 
        'redirect': '/mesero/mesas/'
    })

# ─────────────────────────────────────────────────────────────────────────────
# PATCH /api/comandas/linea/<linea_id>/editar/
# ─────────────────────────────────────────────────────────────────────────────

@csrf_exempt
@api_view(['PATCH', 'DELETE'])
@permission_classes([EsMozoOAdmin])
def api_linea_detail(request, pk):
    """
    Edita o elimina una LineaComanda.
    REGLA DE NEGOCIO: Solo se permite si el estado es 'PENDIENTE'.
    """
    try:
        linea = LineaComanda.objects.select_related('comanda').get(pk=pk)
    except LineaComanda.DoesNotExist:
        return Response({'error': 'Línea no encontrada'}, status=status.HTTP_404_NOT_FOUND)

    # 1. Verificar REGLA DE NEGOCIO
    if linea.estado != LineaComanda.Estado.PENDIENTE:
        return Response({
            'error': f'No se puede modificar: El plato ya está en estado {linea.get_estado_display().upper()}.'
        }, status=status.HTTP_400_BAD_REQUEST)

    if request.method == 'DELETE':
        with transaction.atomic():
            comanda = linea.comanda
            linea.delete()
            comanda.calcular_totales()
            
            # Si la comanda se quedó sin líneas, anularla automáticamente y liberar mesas
            if comanda.lineas.count() == 0:
                comanda.estado = Comanda.Estado.ANULADA
                comanda.save(update_fields=['estado'])
                for m in comanda.todas_las_mesas:
                    m.estado = Mesa.Estado.LIBRE
                    m.save(update_fields=['estado'])
                    
        return Response({'ok': True, 'message': 'Plato eliminado del pedido.'})

    if request.method == 'PATCH':
        data = request.data
        with transaction.atomic():
            if 'plato_id' in data:
                try:
                    nuevo_plato = Plato.objects.get(pk=data['plato_id'])
                    linea.plato = nuevo_plato
                    linea.precio_unitario = nuevo_plato.precio_actual
                except Plato.DoesNotExist:
                    return Response({'error': 'El plato seleccionado no existe'}, status=status.HTTP_404_NOT_FOUND)
            
            if 'cantidad' in data:
                linea.cantidad = max(1, int(data['cantidad']))
            
            if 'notas' in data:
                linea.observacion = data['notas']
            
            # Recalcular siempre por si cambió el plato o la cantidad
            linea.subtotal = linea.precio_unitario * linea.cantidad
            linea.save()
            linea.comanda.calcular_totales()

        return Response({
            'ok': True,
            'linea_id': linea.pk,
            'plato_nombre': linea.plato.nombre,
            'cantidad': linea.cantidad,
            'subtotal': str(linea.subtotal)
        })


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/mesas/<mesa_id>/liberar/
# ─────────────────────────────────────────────────────────────────────────────

@csrf_exempt
@require_POST
def api_liberar_mesa(request, mesa_id):
    """
    Cierra la comanda activa de la mesa y la pone en estado LIBRE.
    Típicamente se llama al presionar "Liberar Mesa / Cobrar".
    """
    try:
        mesa = Mesa.objects.get(pk=mesa_id)
    except Mesa.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Mesa no encontrada'}, status=404)

    with transaction.atomic():
        comanda = obtener_comanda_activa_mesa(mesa)
        if not comanda:
            try:
                comanda = asegurar_comanda_desde_reserva(mesa, request.user)
            except ValueError as exc:
                return JsonResponse({'ok': False, 'error': str(exc)}, status=400)

        if comanda:
            if comanda.estado in (Comanda.Estado.ABIERTA, Comanda.Estado.EN_PREPARACION):
                comanda.estado = Comanda.Estado.LISTA
                comanda.save(update_fields=['estado'])
                
                # Cambiar todas las mesas vinculadas (uniones) a estado POR_PAGAR
                for m in comanda.todas_las_mesas:
                    m.estado = Mesa.Estado.POR_PAGAR
                    m.save(update_fields=['estado'])
                
                return JsonResponse({'ok': True, 'message': 'Comanda enviada a caja. Mesa en espera de pago.'})
            
            # Si ya estaba LISTA, devolvemos éxito igual para evitar el error 400 en el frontend
            # Aseguramos que las mesas estén en POR_PAGAR por si acaso
            for m in comanda.todas_las_mesas:
                if m.estado != Mesa.Estado.POR_PAGAR:
                    m.estado = Mesa.Estado.POR_PAGAR
                    m.save(update_fields=['estado'])
            
            return JsonResponse({'ok': True, 'message': 'La comanda ya está en caja.'})
        
        return JsonResponse({'ok': False, 'error': 'No hay comanda activa en esta mesa.'}, status=400)


@csrf_exempt
@api_view(['POST'])
@permission_classes([EsMozoOAdmin])
def api_marcar_pedido_entregado(request, pk):
    """
    Marca como ENTREGADO todas las líneas en estado LISTO de una comanda.
    Este paso lo ejecuta el mozo al entregar físicamente el pedido al cliente.
    """
    try:
        comanda = Comanda.objects.get(pk=pk)
    except Comanda.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Comanda no encontrada'}, status=404)

    with transaction.atomic():
        lineas_listas = comanda.lineas.filter(estado=LineaComanda.Estado.LISTO)
        cantidad = lineas_listas.count()
        if cantidad == 0:
            return JsonResponse(
                {'ok': False, 'error': 'No hay platos listos pendientes de entrega.'},
                status=400
            )

        ahora = timezone.now()
        
        from apps.menu.services import descontar_stock_plato
        # Convertir a lista para usar las instancias en el loop
        for linea in list(lineas_listas):
            descontar_stock_plato(linea, request.user)

        lineas_listas.update(
            estado=LineaComanda.Estado.ENTREGADO,
            fecha_entregado=ahora,
        )


        comanda.marcar_como_lista()

    return JsonResponse({
        'ok': True,
        'lineas_entregadas': cantidad,
        'message': 'Pedido marcado como entregado.'
    })



# ─────────────────────────────────────────────────────────────────────────────
# POST /api/comandas/<comanda_id>/platos/
# ─────────────────────────────────────────────────────────────────────────────
@csrf_exempt
@api_view(['POST'])
@permission_classes([EsMozoOAdmin])
def api_agregar_plato_comanda(request, pk):
    """
    Agrega un plato a una comanda existente.
    """
    data = request.data

    try:
        comanda = Comanda.objects.get(pk=pk)
    except Comanda.DoesNotExist:
        return JsonResponse({'error': 'Comanda no encontrada'}, status=404)

    plato_id = data.get('plato_id')
    cantidad = int(data.get('cantidad', 1))

    try:
        plato = Plato.objects.get(pk=plato_id)
    except Plato.DoesNotExist:
        return JsonResponse({'error': 'Plato no encontrado'}, status=404)

    apto, error_data = verificar_stock_plato(plato, cantidad)
    if not apto:
        return JsonResponse(error_data, status=400)

    linea = LineaComanda.objects.create(
        comanda=comanda,
        plato=plato,
        cantidad=cantidad,
        precio_unitario=plato.precio_actual,
        subtotal=plato.precio_actual * cantidad,
        observacion=data.get('notas', ''),
        fecha_envio_cocina=timezone.now(),
        fecha_inicio_prep=timezone.now(),
        tiempo_estimado_min=plato.tiempo_preparacion_min or 0,
        estado=LineaComanda.Estado.EN_PREP,
    )

    # Calcular totales
    comanda.calcular_totales()

    # Notificar al KDS
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            'kds_updates',
            {'type': 'kds_update', 'action': 'nueva_linea', 'detail': {'comanda_id': comanda.id}}
        )
    except Exception:
        pass

    return JsonResponse({'ok': True, 'linea_id': linea.id})

# ─────────────────────────────────────────────────────────────────────────────
# KDS API (Phase 4)
# ─────────────────────────────────────────────────────────────────────────────
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from django.db.models import Prefetch

@api_view(['GET'])
@permission_classes([EsCocineroOAdmin])
def api_cocina_pendientes(request):
    """
    GET /api/cocina/pendientes/
    Retorna comandas con líneas PENDIENTE o EN_PREP.
    Optimizado con select_related y prefetch_related.
    """
    lineas_cocina = Prefetch(
        'lineas',
        queryset=LineaComanda.objects.filter(estado__in=[LineaComanda.Estado.PENDIENTE, LineaComanda.Estado.EN_PREP]).select_related('plato'),
        to_attr='lineas_activas'
    )
    
    comandas = Comanda.objects.filter(
        estado=Comanda.Estado.ABIERTA,
        lineas__estado__in=[LineaComanda.Estado.PENDIENTE, LineaComanda.Estado.EN_PREP]
    ).select_related('mesa', 'mesa__zona', 'mozo').prefetch_related(lineas_cocina).distinct()

    data = []
    for c in comandas:
        if not c.lineas_activas: continue
        comanda_data = {
            'id': c.id,
            'mesa_numero': c.mesa.numero,
            'nombre_cliente': c.nombre_cliente or '',
            'zona_nombre': c.mesa.zona.nombre if c.mesa.zona else '',
            'mesero_nombre': c.mozo.username if c.mozo else 'Desconocido',
            'lineas': []
        }
        for l in c.lineas_activas:
            # Calcular tiempo transcurrido (asumimos fecha_envio_cocina es la de creación o similar, 
            # en el modelo original puede no estar explícito, usamos creation date o agregamos fecha_envio_cocina)
            # Wait, the model might not have fecha_envio_cocina. Let's use 'fecha_creacion' if it doesn't exist, or we check the model.
            # Assuming 'fecha_creacion' is there.
            comanda_data['lineas'].append({
                'id': l.id,
                'plato_nombre': l.plato.nombre,
                'cantidad': l.cantidad,
                'notas': getattr(l, 'observacion', getattr(l, 'notas_cocina', '')),
                'estado': l.estado,
                'tiempo_preparacion_min': l.plato.tiempo_preparacion_min,
                # Usa un campo de fecha que exista, o timezone.now() si no
                'fecha_envio_cocina': getattr(c, 'fecha_creacion', getattr(c, 'fecha_apertura', timezone.now())).isoformat() 
            })
        data.append(comanda_data)
        
    return Response(data)

@api_view(['PATCH'])
@permission_classes([EsCocineroOAdmin])
def api_linea_estado(request, pk):
    """
    PATCH /api/lineas/{id}/estado/
    Actualiza el estado de una línea de comanda.
    Permisos: 
    - Cualquiera: PENDIENTE -> EN_PREP
    - Solo COCINERO: EN_PREP -> LISTO
    """
    nuevo_estado = request.data.get('estado')

    with transaction.atomic():
        try:
            linea = LineaComanda.objects.select_for_update(of=('self',)).select_related('plato', 'comanda').get(pk=pk)
        except LineaComanda.DoesNotExist:
            return Response({'error': 'Línea no encontrada'}, status=status.HTTP_404_NOT_FOUND)

        # Validar transiciones y permisos
        if linea.estado == LineaComanda.Estado.PENDIENTE and nuevo_estado == LineaComanda.Estado.EN_PREP:
            if hasattr(linea, 'fecha_inicio_prep'):
                linea.fecha_inicio_prep = timezone.now()

        elif linea.estado == LineaComanda.Estado.EN_PREP and nuevo_estado == LineaComanda.Estado.LISTO:
            if request.user.rol.nombre not in ['COCINERO', 'ADMIN']:
                return Response({'error': 'Solo cocina puede marcar platos como listos'}, status=status.HTTP_403_FORBIDDEN)
            apto, error_data = verificar_stock_plato(linea.plato, linea.cantidad)
            if not apto:
                return Response(error_data, status=status.HTTP_400_BAD_REQUEST)
            if hasattr(linea, 'fecha_listo'):
                linea.fecha_listo = timezone.now()
        else:
            return Response({'error': 'Transición no permitida'}, status=status.HTTP_400_BAD_REQUEST)

        linea.estado = nuevo_estado
        linea.save()

        if nuevo_estado == LineaComanda.Estado.LISTO:
            try:
                from apps.inventario.services import descontar_inventario_al_marcar_listo
                descontar_inventario_al_marcar_listo(linea, request.user)
            except ValueError as exc:
                return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            except Exception:
                logger.exception('Error al descontar inventario para línea %s', pk)
            linea.comanda.marcar_como_lista()

    return Response({'ok': True, 'id': linea.id, 'estado': linea.estado})

# ─────────────────────────────────────────────────────────────────────────────
# KDS HTML View
# ─────────────────────────────────────────────────────────────────────────────
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from apps.mesas.models import Zona

@login_required
@rol_requerido('COCINERO', 'ADMIN')
def kds_view(request):
    """Renderiza el template del Kitchen Display System."""
    zonas = Zona.objects.filter(activo=True).order_by('nombre')
    return render(request, 'cocina/kds.html', {'zonas': zonas})

@login_required
@rol_requerido('COCINERO', 'ADMIN')
def stock_platos_view(request):
    """Renderiza el panel de gestión de stock de platos."""
    return render(request, 'cocina/stock_platos.html')


# ─────────────────────────────────────────────────────────────────────────────
# KDS API — comandas-activas (reemplaza el endpoint legacy)
# ─────────────────────────────────────────────────────────────────────────────

ESTADOS_LINEA_KDS = [
    LineaComanda.Estado.PENDIENTE,
    LineaComanda.Estado.EN_PREP,
    LineaComanda.Estado.LISTO,
]


def _queryset_comandas_kds(zona_id=None):
    lineas_filter = LineaComanda.objects.exclude(
        estado__in=[LineaComanda.Estado.ENTREGADO, LineaComanda.Estado.ANULADO]
    ).select_related('plato')
    lineas_cocina = Prefetch('lineas', queryset=lineas_filter, to_attr='lineas_activas')

    qs = Comanda.objects.filter(
        estado__in=[Comanda.Estado.ABIERTA, Comanda.Estado.EN_PREPARACION],
    ).select_related('mesa', 'mesa__zona', 'mozo').prefetch_related(lineas_cocina).distinct()

    if zona_id:
        qs = qs.filter(mesa__zona_id=zona_id)
    return qs


@api_view(['GET'])
@permission_classes([EsCocineroOAdmin])
def api_cocina_activas(request):
    """
    GET /api/cocina/comandas-activas/?zona=<id>
    Retorna comandas activas (ABIERTA o EN_PREPARACION) con líneas relevantes.
    Serializa los campos exactos que kds.js necesita.
    """
    zona_id = request.GET.get('zona')
    qs = _queryset_comandas_kds(zona_id)

    ahora = timezone.now()
    data = []
    for idx, c in enumerate(qs, start=1):
        lineas_visibles = []
        if c.lineas_activas:
            lineas_visibles = [
                l for l in c.lineas_activas
                if l.estado in ESTADOS_LINEA_KDS
            ]

        # Detectar urgencia: alguna línea supera su tiempo estimado
        tiene_urgencia = False
        lineas_data = []
        for orden, l in enumerate(lineas_visibles, start=1):
            # Tiempo transcurrido desde que se empezó a preparar
            if l.estado == LineaComanda.Estado.EN_PREP and l.fecha_inicio_prep:
                diff_mins = (ahora - l.fecha_inicio_prep).total_seconds() / 60
            else:
                diff_mins = (ahora - l.created_at).total_seconds() / 60

            # Usar el tiempo estimado de la línea (campo propio del modelo)
            tiempo_estimado = l.tiempo_estimado_min or 0
            if tiempo_estimado > 0 and diff_mins > tiempo_estimado:
                tiene_urgencia = True

            estado_display_map = {
                LineaComanda.Estado.PENDIENTE: 'PENDIENTE',
                LineaComanda.Estado.EN_PREP: 'EN PREP',
                LineaComanda.Estado.LISTO: 'LISTO',
                LineaComanda.Estado.ENTREGADO: 'ENTREGADO',
                LineaComanda.Estado.ANULADO: 'ANULADO',
            }

            lineas_data.append({
                'id': l.id,
                'plato_nombre': l.plato.nombre,
                'cantidad': l.cantidad,
                'estado': l.estado,
                'estado_display': estado_display_map.get(l.estado, l.estado),
                'tiempo_transcurrido_min': int(diff_mins),
                'tiempo_estimado': tiempo_estimado,
                'fecha_inicio_prep_iso': l.fecha_inicio_prep.isoformat() if l.fecha_inicio_prep else None,
                'fecha_envio_cocina_iso': l.fecha_envio_cocina.isoformat() if l.fecha_envio_cocina else None,
                'tiempo_real_preparacion_seg': l.tiempo_real_preparacion_seg,
                'orden_entrega': orden,
                'observacion': l.observacion or '',
            })

        data.append({
            'id': c.id,
            'numero_pedido': idx,
            'codigo_comanda': c.codigo_comanda,
            'mesa_numero': c.mesa.numero,
            'zona_nombre': c.mesa.zona.nombre if c.mesa.zona else '',
            'zona_id': c.mesa.zona_id if c.mesa.zona else None,
            'mozo_nombre': c.mozo.username if c.mozo else 'Desconocido',
            'nombre_cliente': c.nombre_cliente or '',
            'estado': c.estado,
            'fecha_apertura': c.fecha_apertura.isoformat(),
            'observacion_general': c.observacion_general or '',
            'tiene_urgencia': tiene_urgencia,
            'lineas': lineas_data,
        })

    return Response(data)


@api_view(['PATCH'])
@permission_classes([EsCocineroOAdmin])
def api_cocina_cambiar_estado(request, pk):
    """
    PATCH /api/cocina/lineas/<id>/cambiar-estado/
    Body: { "nuevo_estado": "EN_PREP"|"LISTO"|"ANULADO", "motivo": "...", "cantidad_parcial": 0 }
    Compatible con kds.js — usa 'nuevo_estado' en lugar de 'estado'.
    cantidad_parcial: si > 0, indica cuántas unidades el cocinero SÍ puede preparar.
    """
    nuevo_estado = request.data.get('nuevo_estado')
    motivo = request.data.get('motivo', '')
    cantidad_parcial = int(request.data.get('cantidad_parcial', 0))

    with transaction.atomic():
        try:
            linea = LineaComanda.objects.select_for_update(of=('self',)).select_related('plato', 'comanda', 'comanda__mesa').get(pk=pk)
        except LineaComanda.DoesNotExist:
            return Response({'error': 'Línea no encontrada'}, status=status.HTTP_404_NOT_FOUND)

        estado_actual = linea.estado

        # Validar transiciones permitidas
        transiciones_ok = {
            LineaComanda.Estado.PENDIENTE: [LineaComanda.Estado.EN_PREP, LineaComanda.Estado.LISTO, LineaComanda.Estado.ANULADO],
            LineaComanda.Estado.EN_PREP:   [LineaComanda.Estado.LISTO, LineaComanda.Estado.ANULADO],
            LineaComanda.Estado.LISTO:     [LineaComanda.Estado.ANULADO],
        }

        permitidos = transiciones_ok.get(estado_actual, [])
        if nuevo_estado not in permitidos:
            return Response({
                'error': f'Transición no permitida: {estado_actual} → {nuevo_estado}'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Validaciones especiales
        if nuevo_estado == LineaComanda.Estado.ANULADO and not motivo:
            return Response({'error': 'Se requiere un motivo para anular.'}, status=status.HTTP_400_BAD_REQUEST)

        if nuevo_estado == LineaComanda.Estado.LISTO:
            if request.user.rol.nombre not in ['COCINERO', 'ADMIN']:
                return Response({'error': 'Solo cocina puede marcar platos como listos'}, status=status.HTTP_403_FORBIDDEN)
            apto, error_data = verificar_stock_plato(linea.plato, linea.cantidad)
            if not apto:
                return Response(error_data, status=status.HTTP_400_BAD_REQUEST)

        # Actualizar timestamps
        ahora = timezone.now()
        if nuevo_estado == LineaComanda.Estado.EN_PREP:
            linea.fecha_inicio_prep = ahora
        elif nuevo_estado == LineaComanda.Estado.LISTO:
            linea.fecha_listo = ahora
            if linea.estado == LineaComanda.Estado.PENDIENTE:
                # Si salta director de Pendiente a Listo, simulamos el inicio.
                linea.fecha_inicio_prep = ahora
            # Auditoría: calcular tiempo real de preparación en segundos
            if linea.fecha_inicio_prep:
                linea.tiempo_real_preparacion_seg = int((ahora - linea.fecha_inicio_prep).total_seconds())

        # ── Cancelación parcial ──────────────────────────────────────────
        nueva_linea_parcial = None
        if nuevo_estado == LineaComanda.Estado.ANULADO:
            linea.motivo_anulacion = motivo
            # Validar cantidad_parcial
            if cantidad_parcial > 0:
                if cantidad_parcial >= linea.cantidad:
                    return Response(
                        {'error': f'La cantidad parcial ({cantidad_parcial}) debe ser menor a la cantidad original ({linea.cantidad}).'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                linea.cantidad_parcial_cocina = cantidad_parcial
                # Crear nueva línea con la cantidad que SÍ se puede cocinar
                nueva_linea_parcial = LineaComanda.objects.create(
                    comanda=linea.comanda,
                    plato=linea.plato,
                    cantidad=cantidad_parcial,
                    precio_unitario=linea.precio_unitario,
                    subtotal=linea.precio_unitario * cantidad_parcial,
                    observacion=linea.observacion,
                    estado=LineaComanda.Estado.EN_PREP,
                    fecha_envio_cocina=linea.fecha_envio_cocina,
                    fecha_inicio_prep=ahora,
                    tiempo_estimado_min=linea.tiempo_estimado_min,
                )

        linea.estado = nuevo_estado
        linea.save()

        # Recalcular totales de la comanda
        linea.comanda.calcular_totales()

        # Auditoría
        from apps.comandas.models import ComandaHistorialEstado
        motivo_historial = motivo or 'Cambio de estado vía KDS'
        if cantidad_parcial > 0 and nuevo_estado == LineaComanda.Estado.ANULADO:
            motivo_historial = f'{motivo} [Parcial: cocinero puede preparar {cantidad_parcial} de {linea.cantidad}]'
        ComandaHistorialEstado.objects.create(
            comanda=linea.comanda,
            estado_anterior=estado_actual,
            estado_nuevo=nuevo_estado,
            usuario=request.user,
            motivo=motivo_historial,
            origen=ComandaHistorialEstado.Origen.KDS,
        )

        # Descontar inventario al marcar LISTO
        if nuevo_estado == LineaComanda.Estado.LISTO:
            from apps.inventario.services import descontar_inventario_al_marcar_listo
            try:
                descontar_inventario_al_marcar_listo(linea, request.user)
            except ValueError as exc:
                return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            except Exception:
                logger.exception('Error al descontar inventario para línea %s (KDS)', pk)

        # Verificar si la comanda completa está lista
        linea.comanda.marcar_como_lista()

        # Notificar vía WebSocket al grupo kds_updates
        try:
            from channels.layers import get_channel_layer
            from asgiref.sync import async_to_sync
            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                'kds_updates',
                {
                    'type': 'kds_update',
                    'action': 'estado_cambiado',
                    'detail': {'linea_id': pk, 'nuevo_estado': nuevo_estado},
                }
            )
        except Exception:
            pass  # El WS es opcional; el polling es el fallback

        # ── Notificar cancelación parcial a meseros ──────────────────────
        if nuevo_estado == LineaComanda.Estado.ANULADO and cantidad_parcial > 0:
            try:
                from channels.layers import get_channel_layer
                from asgiref.sync import async_to_sync
                channel_layer = get_channel_layer()
                async_to_sync(channel_layer.group_send)(
                    'notificaciones_mozos',
                    {
                        'type': 'cancelacion_parcial',
                        'mesa': linea.comanda.mesa.numero,
                        'plato': linea.plato.nombre,
                        'cantidad_original': linea.cantidad,
                        'cantidad_preparable': cantidad_parcial,
                        'motivo': motivo,
                    }
                )
            except Exception:
                pass

    mensaje_map = {
        LineaComanda.Estado.EN_PREP: f'Plato "{linea.plato.nombre}" marcado En Preparación',
        LineaComanda.Estado.LISTO:   f'Plato "{linea.plato.nombre}" marcado Listo',
        LineaComanda.Estado.ANULADO: f'Plato "{linea.plato.nombre}" anulado',
    }

    resp_data = {
        'ok': True,
        'id': linea.id,
        'estado': linea.estado,
        'mensaje': mensaje_map.get(nuevo_estado, 'Estado actualizado'),
    }
    if nueva_linea_parcial:
        resp_data['linea_parcial_id'] = nueva_linea_parcial.id
        resp_data['cantidad_parcial'] = cantidad_parcial
        resp_data['mensaje'] = f'Plato "{linea.plato.nombre}" anulado parcialmente. Se prepararán {cantidad_parcial} de {linea.cantidad}.'

    return Response(resp_data)


@api_view(['GET'])
@permission_classes([EsCocineroOAdmin])
def api_cocina_resumen(request):
    """
    GET /api/cocina/resumen/?zona=<id>
    Devuelve el resumen estadístico para la barra lateral del KDS.
    """
    zona_id = request.GET.get('zona')
    qs = _queryset_comandas_kds(zona_id)

    total = qs.count()

    # Urgentes y Recién Llegados
    ahora = timezone.now()
    urgentes = 0
    recien_llegados = 0
    
    for c in qs.prefetch_related('lineas'):
        # Recién llegado: Todas las líneas relevantes para cocina están en PENDIENTE
        lineas_relevantes = [
            l for l in c.lineas.all()
            if l.estado in ESTADOS_LINEA_KDS
        ]
        if lineas_relevantes and all(l.estado == LineaComanda.Estado.PENDIENTE for l in lineas_relevantes):
            recien_llegados += 1
            
        # Urgentes: comandas con alguna línea EN_PREP que supera su tiempo estimado
        for l in c.lineas.all():
            if l.estado == LineaComanda.Estado.EN_PREP:
                est = l.tiempo_estimado_min or 0
                if est > 0 and l.fecha_inicio_prep:
                    mins = (ahora - l.fecha_inicio_prep).total_seconds() / 60
                    if mins > est:
                        urgentes += 1
                        break

    return Response({'total_pedidos': total, 'pedidos_urgentes': urgentes, 'recien_llegados': recien_llegados})

# ─────────────────────────────────────────────────────────────────────────────
# POST /api/comandas/web/crear/
# ─────────────────────────────────────────────────────────────────────────────
@csrf_exempt
@api_view(['POST'])
def api_crear_pedido_web(request):
    """
    Crea una comanda directamente desde el portal web público.
    Soporta pedidos para llevar (RECOJO) o DELIVERY.
    """
    data = request.data
    items = data.get('items', [])
    tipo_pedido = data.get('tipo_pedido', Comanda.TipoPedido.RECOJO)
    nombre_cliente = data.get('nombre_cliente', '')
    observaciones = data.get('observaciones', '')

    if not items:
        return JsonResponse({'ok': False, 'error': 'El pedido no tiene ítems'}, status=400)

    try:
        with transaction.atomic():
            import datetime
            now = datetime.datetime.now()
            count = Comanda.objects.filter(fecha_apertura__date=now.date()).count() + 1
            codigo = f"WEB-{now.strftime('%Y%m%d')}-{count:03d}"

            cliente = None
            if hasattr(request, 'user') and request.user.is_authenticated and hasattr(request.user, 'perfil_cliente'):
                cliente = request.user.perfil_cliente
                nombre_cliente = f"{cliente.nombres} {cliente.apellidos}"

            if not nombre_cliente:
                nombre_cliente = "Cliente Web Anónimo"

            comanda = Comanda.objects.create(
                codigo_comanda = codigo,
                tipo_pedido = tipo_pedido,
                cliente = cliente,
                nombre_cliente = nombre_cliente,
                estado = Comanda.Estado.ABIERTA,
                observacion_general = observaciones,
            )

            ahora_linea = timezone.now()
            for item in items:
                plato_id = item.get('plato_id')
                cantidad = int(item.get('cantidad', 1))
                if not plato_id or cantidad < 1:
                    raise ValueError(f'Ítem inválido: {item}')
                
                plato = Plato.objects.get(pk=plato_id)
                apto, error_data = verificar_stock_plato(plato, cantidad)
                if not apto:
                    return JsonResponse(error_data, status=400)

                LineaComanda.objects.create(
                    comanda = comanda,
                    plato = plato,
                    cantidad = cantidad,
                    precio_unitario = plato.precio_actual,
                    subtotal = plato.precio_actual * cantidad,
                    observacion = item.get('notas', ''),
                    fecha_envio_cocina = ahora_linea,
                    tiempo_estimado_min = plato.tiempo_preparacion_min or 0,
                )

            comanda.calcular_totales()

    except Plato.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Un plato indicado no existe'}, status=404)
    except ValueError as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=400)
    except Exception as e:
        return JsonResponse({'ok': False, 'error': f'Error interno: {str(e)}'}, status=500)

    # Notificar al KDS
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            'kds_updates',
            {
                'type': 'kds_update',
                'action': 'nueva_comanda',
                'detail': {'comanda_id': comanda.id, 'tipo': tipo_pedido},
            }
        )
    except Exception:
        pass

    return JsonResponse({
        'ok': True, 
        'comanda_id': comanda.id, 
        'mensaje': 'Pedido web creado exitosamente'
    })
