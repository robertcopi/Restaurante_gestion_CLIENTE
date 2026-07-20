from django.db import models
from django.conf import settings
from datetime import datetime, timedelta
from apps.mesas.models import Mesa

class ConfiguracionReserva(models.Model):
    """
    Configuración global de horarios para reservas. 
    Se asumirá que solo existe un registro activo.
    """
    hora_apertura = models.TimeField(verbose_name="Hora de inicio de reservas")
    hora_cierre = models.TimeField(verbose_name="Hora de fin de reservas")
    intervalo_minutos = models.PositiveIntegerField(default=30, verbose_name="Intervalo entre reservas (minutos)")
    capacidad_maxima_personas = models.PositiveIntegerField(
        default=50, 
        verbose_name="Capacidad máxima simultánea"
    )
    tolerancia_no_show_minutos = models.PositiveIntegerField(
        default=15,
        verbose_name="Minutos de espera por no asistencia"
    )
    numero_yape = models.CharField(max_length=20, blank=True, default='987 654 321', verbose_name="Número Yape")
    numero_plin = models.CharField(max_length=20, blank=True, default='987 654 321', verbose_name="Número Plin")

    class Meta:
        verbose_name = "Configuración de Reserva"
        verbose_name_plural = "Configuraciones de Reservas"

    def __str__(self):
        return f"Configuración: {self.hora_apertura} - {self.hora_cierre}"


class DiaNoLaborable(models.Model):
    """
    Fechas específicas donde no se admiten reservas.
    """
    fecha = models.DateField(unique=True, verbose_name="Fecha bloqueada")
    motivo = models.CharField(max_length=255, verbose_name="Motivo (Ej. Feriado, Cierre)")

    class Meta:
        verbose_name = "Día No Laborable"
        verbose_name_plural = "Días No Laborables"

    def __str__(self):
        return f"{self.fecha} - {self.motivo}"


class Reserva(models.Model):
    ESTADOS = (
        ('PENDIENTE', 'Pendiente'),
        ('CONFIRMADA', 'Confirmada'),
        ('CANCELADA', 'Cancelada'),
        ('REPROGRAMADA', 'Reprogramada'),
        ('FINALIZADA', 'Finalizada'),
        ('NO_ASISTIO', 'No asistió'),
        ('EN_MESA', 'En mesa'),
    )

    # Relación formal con el Cliente
    cliente = models.ForeignKey(
        'clientes.Cliente', 
        on_delete=models.CASCADE, 
        null=True, 
        blank=True, 
        related_name='reservas',
        verbose_name="Cliente Asociado"
    )

    # Datos rápidos del cliente (para reservas sin perfil formal o invitados)
    cliente_nombre = models.CharField(max_length=150, verbose_name="Nombre del Cliente")
    cliente_dni = models.CharField(max_length=20, blank=True, null=True, verbose_name="DNI / Documento")
    cliente_telefono = models.CharField(max_length=20, verbose_name="Teléfono")
    cliente_email = models.EmailField(blank=True, null=True, verbose_name="Correo Electrónico")

    # Datos de la reserva
    cantidad_personas = models.PositiveIntegerField(verbose_name="Cantidad de Personas")
    fecha = models.DateField(verbose_name="Fecha de Reserva")
    hora = models.TimeField(verbose_name="Hora de Reserva")
    
    # Asignación y Estado
    mesa = models.ForeignKey(
        Mesa, 
        on_delete=models.SET_NULL, 
        blank=True, 
        null=True, 
        verbose_name="Mesa Asignada"
    )
    estado = models.CharField(
        max_length=20, 
        choices=ESTADOS, 
        default='PENDIENTE', 
        verbose_name="Estado"
    )
    
    observaciones = models.TextField(blank=True, null=True, verbose_name="Observaciones")
    pedido_confirmado = models.BooleanField(default=False, verbose_name="Pedido confirmado")
    cliente_llego = models.BooleanField(default=False, verbose_name="Llegada confirmada")
    fecha_llegada = models.DateTimeField(null=True, blank=True, verbose_name="Fecha de llegada")
    comanda = models.ForeignKey(
        'comandas.Comanda',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reserva_origen',
        verbose_name="Comanda generada",
    )

    # Auditoría basica
    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name="reservas_creadas",
        verbose_name="Creado o Registrado Por"
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Reserva"
        verbose_name_plural = "Reservas"
        ordering = ['-fecha', '-hora']

    def __str__(self):
        return f"Reserva {self.cliente_nombre} - {self.fecha} {self.hora}"

    @property
    def total_pedido(self):
        return sum(item.subtotal for item in self.platos.all())

    @property
    def limite_espera(self):
        from django.utils import timezone
        config = ConfiguracionReserva.objects.first()
        tolerancia = config.tolerancia_no_show_minutos if config else 15
        dt = timezone.make_aware(datetime.combine(self.fecha, self.hora))
        return dt + timedelta(minutes=tolerancia)

    @property
    def puede_confirmar_llegada(self):
        return (
            self.estado == 'CONFIRMADA'
            and self.pedido_confirmado
            and not self.cliente_llego
            and self.mesa_id is not None
        )


class ReservaPlato(models.Model):
    reserva = models.ForeignKey(
        Reserva,
        on_delete=models.CASCADE,
        related_name='platos',
        verbose_name="Reserva",
    )
    plato = models.ForeignKey(
        'menu.Plato',
        on_delete=models.PROTECT,
        related_name='reservas_platos',
        verbose_name="Plato",
    )
    cantidad = models.PositiveIntegerField(default=1, verbose_name="Cantidad")
    precio_unitario = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Precio unitario")

    class Meta:
        verbose_name = "Plato de reserva"
        verbose_name_plural = "Platos de reserva"
        unique_together = ('reserva', 'plato')

    def __str__(self):
        return f"{self.plato.nombre} x{self.cantidad}"

    @property
    def subtotal(self):
        return self.cantidad * self.precio_unitario


class ReservaPago(models.Model):
    METODOS = (
        ('YAPE', 'Yape'),
        ('PLIN', 'Plin'),
        ('TARJETA', 'Tarjeta'),
    )
    ESTADOS = (
        ('PENDIENTE', 'Pendiente'),
        ('PAGADO', 'Pagado'),
    )

    reserva = models.OneToOneField(
        Reserva,
        on_delete=models.CASCADE,
        related_name='pago',
        verbose_name="Reserva",
    )
    metodo = models.CharField(max_length=20, choices=METODOS, verbose_name="Método de pago")
    monto = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Monto")
    estado = models.CharField(max_length=20, choices=ESTADOS, default='PENDIENTE', verbose_name="Estado")
    referencia = models.CharField(max_length=100, blank=True, null=True, verbose_name="Referencia / N° operación")
    ultimos_digitos_tarjeta = models.CharField(max_length=4, blank=True, null=True, verbose_name="Últimos 4 dígitos")
    titular_tarjeta = models.CharField(max_length=120, blank=True, null=True, verbose_name="Titular tarjeta")
    fecha_pago = models.DateTimeField(null=True, blank=True, verbose_name="Fecha de pago")

    class Meta:
        verbose_name = "Pago de reserva"
        verbose_name_plural = "Pagos de reservas"

    def __str__(self):
        return f"Pago {self.get_metodo_display()} — Reserva #{self.reserva_id}"
