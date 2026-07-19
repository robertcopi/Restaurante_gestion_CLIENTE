from django.db import models
from django.conf import settings
from apps.mesas.models import Mesa
from apps.menu.models import Plato

class Comanda(models.Model):
    """Orden de servicio según el esquema SQL."""

    class Estado(models.TextChoices):
        ABIERTA         = 'ABIERTA',        'Abierta'
        EN_PREPARACION  = 'EN_PREPARACION', 'En Preparación'
        LISTA           = 'LISTA',          'Lista para cobrar'
        COBRADA         = 'COBRADA',        'Cobrada'
        ANULADA         = 'ANULADA',        'Anulada'

    class TipoPedido(models.TextChoices):
        LOCAL    = 'LOCAL',    'Consumo en Local'
        RECOJO   = 'RECOJO',   'Recojo en Local'
        DELIVERY = 'DELIVERY', 'Delivery'

    codigo_comanda = models.CharField(max_length=30, unique=True)
    tipo_pedido = models.CharField(max_length=20, choices=TipoPedido.choices, default=TipoPedido.LOCAL)
    mesa = models.ForeignKey(Mesa, on_delete=models.PROTECT, related_name='comandas', null=True, blank=True)
    mesas_adicionales = models.ManyToManyField(Mesa, related_name='uniones_adicionales', blank=True)
    mozo = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='comandas', null=True, blank=True)
    cliente = models.ForeignKey('clientes.Cliente', on_delete=models.SET_NULL, related_name='comandas_web', null=True, blank=True)
    nombre_cliente = models.CharField(max_length=100, blank=True, null=True)
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.ABIERTA)
    
    fecha_apertura = models.DateTimeField(auto_now_add=True)
    fecha_cierre = models.DateTimeField(null=True, blank=True)
    
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    impuesto = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    descuento_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    observacion_general = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'comanda'
        ordering = ['-fecha_apertura']
        verbose_name = 'Comanda'
        verbose_name_plural = 'Comandas'

    def __str__(self):
        return f'Comanda {self.codigo_comanda} — Mesa {self.mesa.numero}'

    def calcular_totales(self):
        """Calcula el total de la comanda basándose en sus líneas."""
        from django.db.models import Sum
        # Aseguramos que usamos los valores correctos de los choices
        self.subtotal = self.lineas.exclude(estado=LineaComanda.Estado.ANULADO).aggregate(res=Sum('subtotal'))['res'] or 0
        # El total incluye impuestos si se requiere, por ahora total = subtotal
        self.total = self.subtotal
        self.save(update_fields=['subtotal', 'total'])

    def marcar_como_lista(self):
        """Cambia el estado de la comanda a LISTA si todas las líneas están listas."""
        lineas_pendientes = self.lineas.exclude(estado__in=[LineaComanda.Estado.LISTO, LineaComanda.Estado.ENTREGADO, LineaComanda.Estado.ANULADO]).exists()
        if not lineas_pendientes and self.estado != Comanda.Estado.LISTA:
            self.estado = Comanda.Estado.LISTA
            self.save(update_fields=['estado'])
            return True
        return False

    # Alias para compatibilidad
    @property
    def todas_las_mesas(self):
        """Devuelve la mesa principal más las adicionales."""
        return [self.mesa] + list(self.mesas_adicionales.all())

    @property
    def mesero(self):
        return self.mozo

    @property
    def notas(self):
        return self.observacion_general

    @property
    def lineas_json(self):
        """Devuelve las líneas de la comanda en formato JSON para el frontend."""
        import json
        items = []
        # Solo incluir líneas no anuladas
        for l in self.lineas.exclude(estado=LineaComanda.Estado.ANULADO).select_related('plato'):
            items.append({
                'id': l.id,
                'plato_nombre': l.plato.nombre,
                'cantidad': l.cantidad,
                'subtotal': str(l.subtotal)
            })
        return json.dumps(items)

class LineaComanda(models.Model):
    """Ítem individual dentro de una comanda según el esquema SQL."""

    class Estado(models.TextChoices):
        PENDIENTE = 'PENDIENTE', 'Pendiente'
        EN_PREP   = 'EN_PREP',    'En Preparación'
        LISTO     = 'LISTO',      'Listo'
        ENTREGADO = 'ENTREGADO',  'Entregado'
        ANULADO   = 'ANULADO',    'Anulado'

    comanda = models.ForeignKey(Comanda, on_delete=models.CASCADE, related_name='lineas')
    plato = models.ForeignKey(Plato, on_delete=models.PROTECT, related_name='lineas')
    cantidad = models.IntegerField(default=1)
    precio_unitario = models.DecimalField(max_digits=10, decimal_places=2)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    observacion = models.CharField(max_length=255, blank=True, null=True)
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.PENDIENTE)
    
    tiempo_estimado_min = models.PositiveSmallIntegerField(default=0)
    fecha_envio_cocina = models.DateTimeField(null=True, blank=True)
    fecha_inicio_prep = models.DateTimeField(null=True, blank=True)
    fecha_listo = models.DateTimeField(null=True, blank=True)
    fecha_entregado = models.DateTimeField(null=True, blank=True)
    
    # Auditoría: tiempo real de preparación (segundos) — se calcula al marcar LISTO
    tiempo_real_preparacion_seg = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Segundos reales de cocción (fecha_listo - fecha_inicio_prep). Disponible para auditoría.'
    )
    # Cancelación parcial: cantidad que el cocinero SÍ puede cocinar
    cantidad_parcial_cocina = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text='En cancelación parcial, cantidad que el cocinero indica que puede preparar.'
    )
    # Motivo de anulación persistido en la línea
    motivo_anulacion = models.CharField(
        max_length=255, blank=True, null=True,
        help_text='Motivo de anulación del plato.'
    )
    
    stock_descontado = models.BooleanField(default=False, help_text='Indica si ya se descontó del stock del plato')
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'linea_comanda'
        verbose_name = 'Línea de Comanda'
        verbose_name_plural = 'Líneas de Comanda'

    def __str__(self):
        return f'{self.cantidad}x {self.plato.nombre} [{self.get_estado_display()}]'

    # Alias para compatibilidad
    @property
    def notas_cocina(self):
        return self.observacion

class ComandaHistorialEstado(models.Model):
    """Historial de cambios de estado para auditoría."""
    
    class Origen(models.TextChoices):
        WEB = 'WEB', 'Web'
        API = 'API', 'API'
        KDS = 'KDS', 'KDS'

    comanda = models.ForeignKey(Comanda, on_delete=models.CASCADE, related_name='historial')
    estado_anterior = models.CharField(max_length=20)
    estado_nuevo = models.CharField(max_length=20)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    fecha_cambio = models.DateTimeField(auto_now_add=True)
    motivo = models.CharField(max_length=255, blank=True, null=True)
    origen = models.CharField(max_length=20, choices=Origen.choices)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'comanda_historial_estado'
        verbose_name = 'Historial de Estado'
        verbose_name_plural = 'Historial de Estados'
