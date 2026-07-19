from rest_framework import serializers
from .models import Categoria, Plato, MovimientoPlato
from apps.inventario.models import RecetaInsumo, Insumo

class RecetaInsumoSerializer(serializers.ModelSerializer):
    """Serializer para ingredientes de recetas en contexto de edición de platos."""
    insumo_id   = serializers.IntegerField()                     # readable + writable
    insumo_nombre = serializers.CharField(source='insumo.nombre', read_only=True)
    insumo_unidad = serializers.CharField(source='insumo.unidad_medida.abreviatura', read_only=True)
    insumo_stock  = serializers.DecimalField(source='insumo.stock_real', max_digits=12, decimal_places=3, read_only=True)

    class Meta:
        model = RecetaInsumo
        fields = ['id', 'insumo_id', 'insumo_nombre', 'insumo_unidad', 'insumo_stock',
                  'cantidad_por_porcion', 'merma_porcentaje', 'activo']

    def create(self, validated_data):
        return RecetaInsumo.objects.create(**validated_data)

    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance

class CategoriaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Categoria
        fields = '__all__'

class MovimientoPlatoSerializer(serializers.ModelSerializer):
    usuario_nombre = serializers.CharField(source='usuario.username', read_only=True)
    plato_nombre = serializers.CharField(source='plato.nombre', read_only=True)
    
    class Meta:
        model = MovimientoPlato
        fields = '__all__'


class PlatoSerializer(serializers.ModelSerializer):
    """Serializer para platos con información de disponibilidad e insumos."""
    categoria_nombre = serializers.ReadOnlyField(source='categoria.nombre')
    imagen_url = serializers.ReadOnlyField()
    receta = RecetaInsumoSerializer(many=True, read_only=True)
    receta_ids = serializers.PrimaryKeyRelatedField(
        write_only=True,
        many=True,
        queryset=RecetaInsumo.objects.all(),
        source='receta',
        required=False
    )

    class Meta:
        model = Plato
        fields = [
            'id', 'nombre', 'descripcion', 'categoria', 'categoria_nombre',
            'precio_actual', 'tiempo_preparacion_min', 'imagen', 'imagen_url',
            'disponible', 'activo', 'control_stock', 'stock_actual', 'stock_minimo', 
            'receta', 'receta_ids',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at', 'imagen_url', 'stock_actual']
