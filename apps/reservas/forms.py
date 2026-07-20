from django import forms
from django.core.exceptions import ValidationError

from apps.mesas.models import Mesa
from .models import Reserva, ConfiguracionReserva, DiaNoLaborable
from .services import (
    mesa_ocupada_en_horario,
    mesas_disponibles_para,
    parse_fecha_hora_form,
)


class ReservaForm(forms.ModelForm):
    class Meta:
        model = Reserva
        fields = [
            'cliente_nombre', 'cliente_dni', 'cliente_telefono', 'cliente_email',
            'cantidad_personas', 'fecha', 'hora', 'mesa', 'observaciones',
        ]
        widgets = {
            'fecha': forms.DateInput(attrs={'type': 'date', 'class': 'reserva-fecha-input'}),
            'hora': forms.TimeInput(attrs={'type': 'time', 'class': 'reserva-hora-input'}),
            'cantidad_personas': forms.NumberInput(attrs={'min': '1', 'max': '6', 'class': 'form-control'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        fecha, hora = parse_fecha_hora_form(
            self.data.get('fecha') if self.data else None,
            self.data.get('hora') if self.data else None,
        )
        if not fecha and self.initial:
            fecha, hora = parse_fecha_hora_form(
                self.initial.get('fecha'),
                self.initial.get('hora'),
            )

        exclude_pk = self.instance.pk if self.instance and self.instance.pk else None
        if fecha and hora:
            self.fields['mesa'].queryset = mesas_disponibles_para(fecha, hora, exclude_pk)
        else:
            self.fields['mesa'].queryset = Mesa.objects.none()
            self.fields['mesa'].empty_label = 'Seleccione fecha y hora primero'

        self.fields['mesa'].label_from_instance = (
            lambda mesa: f"Mesa {mesa.numero} ({mesa.zona.nombre}) — {mesa.capacidad} pers."
        )
        self.fields['mesa'].widget.attrs.setdefault('class', 'form-select reserva-mesa-input')

    def clean(self):
        cleaned_data = super().clean()
        fecha = cleaned_data.get('fecha')
        hora = cleaned_data.get('hora')
        mesa = cleaned_data.get('mesa')

        if fecha and hora:
            if DiaNoLaborable.objects.filter(fecha=fecha).exists():
                raise ValidationError(
                    "La fecha seleccionada no está disponible para reservas (Día No Laborable o Bloqueado)."
                )

            config = ConfiguracionReserva.objects.first()
            if config:
                if hora < config.hora_apertura or hora > config.hora_cierre:
                    raise ValidationError(
                        f"La hora de la reserva debe estar dentro del horario de atención: "
                        f"de {config.hora_apertura.strftime('%H:%M')} a {config.hora_cierre.strftime('%H:%M')}."
                    )

            if mesa:
                exclude_pk = self.instance.pk if self.instance and self.instance.pk else None
                if mesa_ocupada_en_horario(mesa.pk, fecha, hora, exclude_pk):
                    raise ValidationError(
                        f"La mesa {mesa.numero} ya está reservada en ese horario. Elija otra mesa u horario."
                    )

        return cleaned_data
