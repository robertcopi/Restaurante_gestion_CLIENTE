"""
API endpoint: GET /api/menu/catalogo/
Devuelve el catálogo completo de platos agrupados por categoría.
Solo incluye platos con disponible=True.
"""
import json
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.exceptions import ValidationError

from apps.usuarios.permissions import EsAdmin
from apps.usuarios.utils import log_auditoria
from apps.inventario.services import obtener_insumos_criticos
from apps.inventario.models import RecetaInsumo
from .models import Categoria, Plato
from .serializers import CategoriaSerializer, PlatoSerializer

class CategoriaViewSet(viewsets.ModelViewSet):
    queryset = Categoria.objects.all().order_by('orden', 'nombre')
    serializer_class = CategoriaSerializer
    permission_classes = [IsAuthenticated, EsAdmin]

    def perform_create(self, serializer):
        instance = serializer.save()
        log_auditoria(self.request.user, 'CREACION', 'CATEGORIA', instance.id, 
                      detalle_nuevo=serializer.data, request=self.request)

    def perform_update(self, serializer):
        old_instance = self.get_object()
        old_data = CategoriaSerializer(old_instance).data
        instance = serializer.save()
        log_auditoria(self.request.user, 'EDICION', 'CATEGORIA', instance.id, 
                      detalle_anterior=old_data, detalle_nuevo=serializer.data, request=self.request)

    def perform_destroy(self, instance):
        if instance.platos.exists():
            raise ValidationError({'detail': 'No se puede eliminar esta categoría porque tiene platos asociados.'})

        old_data = CategoriaSerializer(instance).data
        instance_id = instance.id
        instance.delete()
        log_auditoria(self.request.user, 'ELIMINACION', 'CATEGORIA', instance_id, 
                      detalle_anterior=old_data, request=self.request)

class PlatoViewSet(viewsets.ModelViewSet):
    queryset = Plato.objects.prefetch_related('receta__insumo__unidad_medida').order_by('categoria__orden', 'nombre')
    serializer_class = PlatoSerializer
    permission_classes = [IsAuthenticated, EsAdmin]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def perform_create(self, serializer):
        instance = serializer.save()
        # Asignar recetas si vienen en los datos
        self._asignar_recetas(instance)
        log_auditoria(self.request.user, 'CREACION', 'PLATOS', instance.id, 
                      detalle_nuevo=PlatoSerializer(instance).data, request=self.request)

    def perform_update(self, serializer):
        # Obtener detalle anterior
        old_instance = self.get_object()
        old_data = PlatoSerializer(old_instance).data
        instance = serializer.save()
        # Asignar recetas si vienen en los datos
        self._asignar_recetas(instance)
        log_auditoria(self.request.user, 'EDICION', 'PLATOS', instance.id, 
                      detalle_anterior=old_data, detalle_nuevo=PlatoSerializer(instance).data, request=self.request)

    def perform_destroy(self, instance):
        old_data = PlatoSerializer(instance).data
        instance_id = instance.id
        instance.delete()
        log_auditoria(self.request.user, 'ELIMINACION', 'PLATOS', instance_id, 
                      detalle_anterior=old_data, request=self.request)

    def _asignar_recetas(self, plato):
        """
        Actualiza la receta del plato con los insumos del request.
        Usa update_or_create para no perder historial — nunca hace delete físico.
        """
        if 'receta' not in self.request.data:
            return

        receta_data = self.request.data.getlist('receta')
        if not receta_data:
            return

        from apps.inventario.models import Insumo
        from django.db import transaction as db_transaction

        with db_transaction.atomic():
            insumo_ids_nuevos = set()

            for item in receta_data:
                try:
                    if isinstance(item, str):
                        item = json.loads(item)
                    if not isinstance(item, dict):
                        continue

                    insumo_id = item.get('insumo_id')
                    cantidad = item.get('cantidad_por_porcion', 1)

                    if not insumo_id:
                        continue
                    if float(cantidad) <= 0:
                        raise ValidationError(f'Cantidad inválida para insumo {insumo_id}')

                    if not Insumo.objects.filter(pk=insumo_id, activo=True).exists():
                        raise ValidationError(f'Insumo {insumo_id} no existe o está inactivo.')

                    RecetaInsumo.objects.update_or_create(
                        plato=plato,
                        insumo_id=insumo_id,
                        defaults={
                            'cantidad_por_porcion': cantidad,
                            'merma_porcentaje': item.get('merma_porcentaje', 0),
                            'activo': True,
                        },
                    )
                    insumo_ids_nuevos.add(int(insumo_id))

                except (json.JSONDecodeError, ValueError):
                    continue

            # Desactivar insumos de la receta que ya no están en la lista nueva
            if insumo_ids_nuevos:
                plato.receta.exclude(insumo_id__in=insumo_ids_nuevos).update(activo=False)

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated, EsAdmin])
    def insumos_criticos(self, request):
        """
        Retorna lista de insumos críticos (con stock bajo o negativo)
        y sus platos afectados.
        """
        criticos = obtener_insumos_criticos()
        return Response({
            'total': len(criticos),
            'insumos': criticos
        })

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, EsAdmin])
    def agregar_insumo(self, request, pk=None):
        """
        Agrega un insumo a la receta del plato.
        POST /api/menu/platos/{id}/agregar_insumo/
        Body: {
            "insumo_id": 1,
            "cantidad_por_porcion": 100,
            "merma_porcentaje": 5
        }
        """
        plato = self.get_object()
        insumo_id = request.data.get('insumo_id')
        cantidad = request.data.get('cantidad_por_porcion')
        merma = request.data.get('merma_porcentaje', 0)
        
        if not insumo_id or not cantidad:
            return Response(
                {'error': 'Se requiere insumo_id y cantidad_por_porcion'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            receta, created = RecetaInsumo.objects.update_or_create(
                plato=plato,
                insumo_id=insumo_id,
                defaults={
                    'cantidad_por_porcion': cantidad,
                    'merma_porcentaje': merma,
                    'activo': True
                }
            )
            return Response({
                'id': receta.id,
                'mensaje': 'Insumo agregado/actualizado en la receta'
            })
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['delete'], permission_classes=[IsAuthenticated, EsAdmin])
    def eliminar_insumo(self, request, pk=None):
        """
        Elimina un insumo de la receta del plato.
        DELETE /api/menu/platos/{id}/eliminar_insumo/?insumo_id={insumo_id}
        """
        plato = self.get_object()
        insumo_id = request.query_params.get('insumo_id')
        
        if not insumo_id:
            return Response(
                {'error': 'Se requiere insumo_id'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            receta = plato.receta.get(insumo_id=insumo_id)
            receta.delete()
            return Response({'mensaje': 'Insumo eliminado de la receta'})
        except RecetaInsumo.DoesNotExist:
            return Response(
                {'error': 'Insumo no encontrado en este plato'},
                status=status.HTTP_404_NOT_FOUND
            )

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, EsAdmin])
    def ajustar_stock(self, request, pk=None):
        """POST /api/menu/platos/{id}/ajustar_stock/"""
        plato = self.get_object()
        
        try:
            cantidad = int(request.data.get('cantidad', 0))
            tipo = request.data.get('tipo', 'AJUSTE')
            motivo = request.data.get('motivo', 'Ajuste manual')
        except ValueError:
            return Response({'error': 'Cantidad inválida'}, status=status.HTTP_400_BAD_REQUEST)
            
        if cantidad == 0:
            return Response({'error': 'La cantidad no puede ser 0'}, status=status.HTTP_400_BAD_REQUEST)
            
        from django.db import transaction
        from apps.menu.models import MovimientoPlato
        
        with transaction.atomic():
            plato = Plato.objects.select_for_update().get(pk=pk)
            plato.stock_actual += cantidad
            
            if plato.stock_actual > 0:
                plato.disponible = True
            elif plato.stock_actual <= 0:
                plato.disponible = False
                
            plato.save(update_fields=['stock_actual', 'disponible'])
            
            MovimientoPlato.objects.create(
                plato=plato,
                usuario=request.user,
                tipo=tipo,
                cantidad=cantidad,
                motivo=motivo
            )
            
        return Response({'mensaje': 'Stock actualizado', 'stock_actual': plato.stock_actual, 'disponible': plato.disponible})

    @action(detail=True, methods=['get'], permission_classes=[IsAuthenticated, EsAdmin])
    def historial_stock(self, request, pk=None):
        """GET /api/menu/platos/{id}/historial_stock/"""
        plato = self.get_object()
        movimientos = plato.movimientos_stock.all()
        from apps.menu.serializers import MovimientoPlatoSerializer
        serializer = MovimientoPlatoSerializer(movimientos, many=True)
        return Response(serializer.data)


@require_GET
def catalogo_api(request):
    """
    Responde con la lista de categorías y sus platos disponibles.
    Formato:
    {
      "categorias": [
        { "id": 1, "nombre": "Entradas", "icono": "bi-egg-fried",
          "platos": [{ "id": 1, "nombre": "...", "precio": "12.50", "imagen": "..." }] }
      ]
    }
    """
    categorias = []
    for cat in Categoria.objects.prefetch_related('platos').all():
        platos_disponibles = cat.platos.filter(disponible=True)
        if platos_disponibles.exists():
            categorias.append({
                'id':     cat.pk,
                'nombre': cat.nombre,
                'icono':  cat.icono,
                'platos': [
                    {
                        'id':          p.pk,
                        'nombre':      p.nombre,
                        'descripcion': p.descripcion,
                        'precio':      str(p.precio),
                        'imagen':      p.imagen_url(),
                        'tiempo_prep': p.tiempo_prep,
                    }
                    for p in platos_disponibles
                ],
            })

    return JsonResponse({'categorias': categorias})
