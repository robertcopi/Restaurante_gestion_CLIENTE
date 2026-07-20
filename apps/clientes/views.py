from django.shortcuts import render, redirect
from django.db import models
from apps.menu.models import Categoria, Plato

from django.contrib.auth import authenticate, login, logout

from django.contrib.auth.decorators import login_required

from django.contrib import messages
from django.utils import timezone

from apps.usuarios.models import Usuario, Rol

from .models import Cliente

from apps.reservas.models import Reserva

from apps.reservas.context_helpers import menu_reserva_context

from apps.reservas.services import (
    parse_platos_post,
    guardar_platos_reserva,
    liberar_mesas_por_no_asistencia,
)

from .forms import ClienteReservaForm



def logout_cliente(request):

    logout(request)

    return redirect('index')



def registro_cliente(request):

    if request.user.is_authenticated:

        return redirect('clientes:mis_reservas')



    if request.method == 'POST':

        username = request.POST.get('username')

        nombres = request.POST.get('nombres')

        apellidos = request.POST.get('apellidos')

        email = request.POST.get('email')

        password = request.POST.get('password')

        dni = request.POST.get('dni', '')

        telefono = request.POST.get('telefono', '')

        if len(dni) != 8 or not dni.isdigit():
            messages.error(request, "El DNI/Documento debe contener exactamente 8 números.")
            return render(request, 'clientes/registro.html')

        if len(telefono) != 9 or not telefono.isdigit():
            messages.error(request, "El teléfono celular debe contener exactamente 9 números.")
            return render(request, 'clientes/registro.html')

        if Usuario.objects.filter(username=username).exists():

            messages.error(request, "El nombre de usuario ya existe.")

            return render(request, 'clientes/registro.html')

            

        if Usuario.objects.filter(email=email).exists():

            messages.error(request, "El correo electrónico ya está registrado.")

            return render(request, 'clientes/registro.html')

        if dni and Usuario.objects.filter(dni=dni).exists():

            messages.error(request, "El DNI ingresado ya se encuentra registrado.")

            return render(request, 'clientes/registro.html')



        try:

            rol_cliente, _ = Rol.objects.get_or_create(nombre='CLIENTE', defaults={'descripcion': 'Cliente del Restaurante'})

            

            user = Usuario.objects.create_user(

                username=username,

                email=email,

                password=password,

                nombres=nombres,

                apellidos=apellidos,

                rol=rol_cliente,

                dni=dni,

                telefono=telefono

            )

            

            Cliente.objects.create(

                usuario=user,

                nombres=nombres,

                apellidos=apellidos,

                numero_documento=dni,

                email=email,

                telefono=telefono

            )

            

            auth_user = authenticate(request, username=username, password=password)

            if auth_user is not None:

                login(request, auth_user)

            else:

                login(request, user)

                

            messages.success(request, f"¡Bienvenido {nombres}! Tu cuenta fue creada exitosamente.")

            return redirect('clientes:mis_reservas')

            

        except Exception as e:

            messages.error(request, f"Ocurrió un error al registrarse. Detalle: {str(e)}")

            return render(request, 'clientes/registro.html')

        

    return render(request, 'clientes/registro.html')



@login_required(login_url='login')

def mis_reservas(request):

    if not hasattr(request.user, 'perfil_cliente'):

        messages.error(request, "Necesita un perfil de cliente para acceder.")

        return redirect('login')



    liberar_mesas_por_no_asistencia()

        

    cliente = request.user.perfil_cliente

    reservas = (

        Reserva.objects.filter(cliente=cliente)

        .select_related('mesa', 'mesa__zona')

        .prefetch_related('platos__plato')

        .order_by('-fecha', '-hora')

    )

    hoy = timezone.localdate()
    proximas_reservas = (
        reservas
        .filter(fecha__gte=hoy)
        .exclude(estado__in=['CANCELADA', 'FINALIZADA', 'NO_ASISTIO'])
        .order_by('fecha', 'hora')
    )
    resumen = {
        'total': reservas.count(),
        'pendientes': reservas.filter(estado='PENDIENTE').count(),
        'confirmadas': reservas.filter(estado='CONFIRMADA').count(),
        'proxima': proximas_reservas.first(),
    }

    

    return render(request, 'clientes/mis_reservas.html', {
        'reservas': reservas,
        'cliente': cliente,
        'resumen': resumen,
    })



def nueva_reserva(request):
    cliente = None
    if request.user.is_authenticated and hasattr(request.user, 'perfil_cliente'):
        cliente = request.user.perfil_cliente

    

    if request.method == 'POST':

        form = ClienteReservaForm(request.POST, cliente=cliente)

        confirmar = request.POST.get('accion') == 'confirmar_pedido'
        platos_cantidades = {plato_id: qty for plato_id, qty in parse_platos_post(request.POST)}

        if form.is_valid():
            reserva = form.save()
            platos_data = parse_platos_post(request.POST)
            if platos_data:
                guardar_platos_reserva(reserva, platos_data)
            if confirmar:
                if not platos_data:
                    messages.error(request, "Debe seleccionar al menos un plato para continuar al pago.")
                else:
                    return redirect('pago_reserva', pk=reserva.pk)
            messages.success(request, "Reserva registrada con éxito.")
            return redirect('index')

        print("ERRORES VALIDACION:", form.errors)
        messages.error(request, "Revisa los datos del formulario e intenta de nuevo.")

    else:
        form = ClienteReservaForm(cliente=cliente)
        platos_cantidades = {}

    context = {
        'cliente': cliente,
        'form': form,
        'platos_cantidades': platos_cantidades,
        **menu_reserva_context(),
    }

    return render(request, 'clientes/nueva_reserva.html', context)


# ─── Vistas Públicas (Web TICUY) ───

def home_publica(request):
    if request.user.is_authenticated:
        if hasattr(request.user, 'perfil_cliente'):
            return redirect('clientes:mis_reservas')
        if hasattr(request.user, 'rol') and request.user.rol:
            return redirect('dashboard')
            
    context = {}
    try:
        context['destacado_lomo'] = Plato.objects.filter(nombre__icontains='lomo').first()
        context['destacado_ceviche'] = Plato.objects.filter(nombre__icontains='ceviche').first()
        context['destacado_pisco'] = Plato.objects.filter(nombre__icontains='pisco').first()
    except Exception:
        pass
        
    return render(request, 'web/index.html', context)

def menu_publico(request):
    categorias = Categoria.objects.filter(activo=True).prefetch_related(
        models.Prefetch('platos', queryset=Plato.objects.filter(activo=True, disponible=True))
    )
    return render(request, 'web/menu.html', {'categorias': categorias})

def carrito_publico(request):
    return render(request, 'web/carrito.html')

def checkout_publico(request):
    cliente = None
    if request.user.is_authenticated and hasattr(request.user, 'perfil_cliente'):
        cliente = request.user.perfil_cliente
    
    return render(request, 'web/checkout.html', {'cliente': cliente})
