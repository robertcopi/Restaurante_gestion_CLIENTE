import io
from django.conf import settings
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import mm
from reportlab.lib.units import mm as mm_unit
from reportlab.lib.colors import HexColor, black, white, gray
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing
from django.utils import timezone

def numero_a_letras(n):
    """Convierte un número a su representación en texto (Español)."""
    unidades = ["", "UN", "DOS", "TRES", "CUATRO", "CINCO", "SEIS", "SIETE", "OCHO", "NUEVE"]
    decenas = ["", "DIEZ", "VEINTE", "TREINTA", "CUARENTA", "CINCUENTA", "SESENTA", "SETENTA", "OCHENTA", "NOVENTA"]
    especiales = {
        11: "ONCE", 12: "DOCE", 13: "TRECE", 14: "CATORCE", 15: "QUINCE",
        16: "DIECISEIS", 17: "DIECISIETE", 18: "DIECIOCHO", 19: "DIECINUEVE",
        21: "VEINTIUNO", 22: "VEINTIDOS", 23: "VEINTITRES", 24: "VEINTICUATRO",
        25: "VEINTICINCO", 26: "VEINTISEIS", 27: "VEINTISIETE", 28: "VEINTIOCHO", 29: "VEINTINUEVE"
    }
    centenas = ["", "CIENTO", "DOSCIENTOS", "TRESCIENTOS", "CUATROCIENTOS", "QUINIENTOS", "SEISCIENTOS", "SETENCIENTOS", "OCHOCIENTOS", "NOVECIENTOS"]

    def convertir_grupo(n):
        if n == 100: return "CIEN"
        resultado = []
        c = n // 100
        d = (n % 100) // 10
        u = n % 10
        
        if c > 0: resultado.append(centenas[c])
        
        if n % 100 in especiales:
            resultado.append(especiales[n % 100])
        else:
            if d > 0:
                if d == 1: # Diez y algo
                    resultado.append(decenas[d]) # No debería llegar aquí por especiales
                elif d == 2: # Veinte y algo
                    resultado.append("VEINTE") # No debería llegar aquí por especiales
                else:
                    if u > 0:
                        resultado.append(f"{decenas[d]} Y {unidades[u]}")
                    else:
                        resultado.append(decenas[d])
            elif u > 0:
                resultado.append(unidades[u])
        
        return " ".join(resultado)

    entero = int(n)
    decimales = int(round((n - entero) * 100))
    
    if entero == 0:
        texto = "CERO"
    elif entero < 1000:
        texto = convertir_grupo(entero)
    elif entero < 1000000:
        miles = entero // 1000
        resto = entero % 1000
        if miles == 1:
            texto = f"MIL {convertir_grupo(resto)}"
        else:
            texto = f"{convertir_grupo(miles)} MIL {convertir_grupo(resto)}"
    else:
        texto = "CANTIDAD MUY GRANDE"

    return f"{texto} Y {decimales:02d}/100 SOLES"


def _obtener_reserva_boleta(pago):
    from apps.reservas.services import obtener_reserva_prepagada_comanda
    return obtener_reserva_prepagada_comanda(pago.comanda)


def _nombre_mesero(comanda):
    mozo = comanda.mozo
    if not mozo:
        return 'SISTEMA'
    nombre = f"{getattr(mozo, 'nombres', '')} {getattr(mozo, 'apellidos', '')}".strip()
    return nombre or getattr(mozo, 'username', 'SISTEMA')


def generar_pdf_boleta(pago, qr_url=None):
    """
    Genera un PDF con el diseño solicitado para RESTAURANT OS.
    """
    from apps.caja.models import Pago
    buffer = io.BytesIO()
    
    ancho_ticket = 80 * mm_unit
    alto_ticket = 250 * mm_unit # Aumentado para el nuevo contenido
    
    c = canvas.Canvas(buffer, pagesize=(ancho_ticket, alto_ticket))
    
    COLOR_TEXT = HexColor("#000000")
    COLOR_GRAY = HexColor("#444444")
    
    y = alto_ticket - 10 * mm_unit
    
    # ─── ENCABEZADO (Basado en el diseño de la imagen) ────────────────────────
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(ancho_ticket / 2, y, "TICUY")
    y -= 6 * mm_unit
    
    c.setFont("Helvetica", 9)
    c.drawCentredString(ancho_ticket / 2, y, "RUC: 20601234567")
    y -= 4 * mm_unit
    c.drawCentredString(ancho_ticket / 2, y, "Jose Leonardo Ortiz 450")
    y -= 8 * mm_unit
    
    # Caja de Boleta Electrónica
    c.setLineWidth(1)
    c.setStrokeColor(COLOR_TEXT)
    c.roundRect(10*mm, y - 6*mm, ancho_ticket - 20*mm, 12*mm, 3, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(ancho_ticket / 2, y + 1*mm, "BOLETA DE VENTA ELECTRÓNICA")
    c.drawCentredString(ancho_ticket / 2, y - 3*mm, f"Nº B001-{pago.id:06d}")
    y -= 15 * mm_unit
    
    # ─── METADATA (Izquierda) ────────────────────────────────────────────────
    c.setFont("Helvetica", 8)
    line_h = 4 * mm

    reserva = _obtener_reserva_boleta(pago)
    reserva_pago = getattr(reserva, 'pago', None) if reserva else None
    es_prepago_reserva = bool(reserva_pago and reserva_pago.estado == 'PAGADO')

    cliente = (
        (reserva.cliente_nombre if reserva else None)
        or pago.observacion
        or pago.comanda.nombre_cliente
        or "PÚBLICO EN GENERAL"
    )
    c.drawString(10*mm, y, f"Cliente : {cliente.upper()}")
    y -= line_h

    mesa = reserva.mesa if reserva and reserva.mesa_id else pago.comanda.mesa
    zona_nombre = mesa.zona.nombre.upper() if mesa.zona else 'SIN ZONA'
    c.drawString(10*mm, y, f"Ubicación : Mesa {mesa.numero} — {zona_nombre}")
    y -= line_h

    mesero = _nombre_mesero(pago.comanda)
    c.drawString(10*mm, y, f"Mesero : {mesero.upper()}")
    y -= line_h

    if es_prepago_reserva and reserva_pago.fecha_pago:
        fecha_hora = timezone.localtime(reserva_pago.fecha_pago)
    else:
        fecha_hora = timezone.localtime(pago.fecha_pago or timezone.now())
    c.drawString(10*mm, y, f"Fecha : {fecha_hora.strftime('%d/%m/%Y')}")
    y -= line_h
    c.drawString(10*mm, y, f"Hora : {fecha_hora.strftime('%H:%M:%S')}")
    y -= line_h
    if es_prepago_reserva:
        c.setFont("Helvetica-Bold", 7)
        c.drawString(10*mm, y, f"Reserva : #{reserva.id} — Pago online ({reserva_pago.get_metodo_display()})")
        y -= line_h
        c.setFont("Helvetica", 8)
    y -= 2 * mm_unit
    
    # ─── TABLA DE PRODUCTOS (Con cuadro bonito) ──────────────────────────────
    c.setDash(2, 1)
    c.line(10*mm, y, ancho_ticket - 10*mm, y)
    c.setDash()
    y -= 4 * mm_unit
    
    c.setFont("Helvetica-Bold", 7)
    c.drawString(10*mm, y, "PRODUCTO")
    c.drawString(45*mm, y, "CANT.")
    c.drawString(55*mm, y, "P.UNIT.")
    c.drawString(68*mm, y, "IMPORTE")
    y -= 2 * mm_unit
    c.line(10*mm, y, ancho_ticket - 10*mm, y)
    y -= 4 * mm_unit
    
    c.setFont("Helvetica", 7)
    if pago.lineas_pagadas.exists():
        lineas = pago.lineas_pagadas.all()
    else:
        lineas = pago.comanda.lineas.exclude(estado='ANULADO')
    for l in lineas:
        nombre = l.plato.nombre
        if len(nombre) > 22: nombre = nombre[:20] + ".."
        c.drawString(10*mm, y, nombre.upper())
        c.drawString(47*mm, y, f"{l.cantidad}")
        c.drawString(55*mm, y, f"{l.precio_unitario:.2f}")
        c.drawRightString(ancho_ticket - 10*mm, y, f"{l.subtotal:.2f}")
        y -= 4 * mm_unit
        
    y -= 2 * mm_unit
    c.line(10*mm, y, ancho_ticket - 10*mm, y)
    y -= 6 * mm_unit
    
    # ─── TOTALES ─────────────────────────────────────────────────────────────
    if pago.transaccion_id:
        pagos_transaccion = list(Pago.objects.filter(transaccion_id=pago.transaccion_id, estado=Pago.Estado.PAGADO).select_related('metodo_pago'))
    else:
        pagos_transaccion = [pago]

    # Calcular el total sumando directamente el costo de los productos del ticket
    monto_total = sum(float(l.subtotal) for l in lineas)
    
    igv = monto_total * 0.18
    subtotal = monto_total - igv
    
    c.setFont("Helvetica", 8)
    c.drawRightString(55*mm, y, "Subtotal")
    c.drawString(58*mm, y, f": S/ {subtotal:.2f}")
    y -= 4 * mm_unit
    c.drawRightString(55*mm, y, "Total I.G.V. (18%)")
    c.drawString(58*mm, y, f": S/ {igv:.2f}")
    y -= 4 * mm_unit
    
    c.setFont("Helvetica-Bold", 9)
    c.drawRightString(55*mm, y, "Total Precio Venta")
    c.drawString(58*mm, y, f": S/ {monto_total:.2f}")
    y -= 6 * mm_unit
    
    # ─── FORMA DE PAGO ───────────────────────────────────────────────────────
    c.setDash(2, 1)
    c.line(10*mm, y, ancho_ticket - 10*mm, y)
    c.setDash()
    y -= 4 * mm_unit
    
    c.setFont("Helvetica-Bold", 8)
    c.drawString(10*mm, y, "FORMA DE PAGO:")
    y -= 4 * mm_unit
    
    c.setFont("Helvetica", 8)
    
    for p_tr in pagos_transaccion:
        try:
            metodo_nombre = p_tr.metodo_pago.nombre.upper()
        except AttributeError:
            metodo_nombre = "EFECTIVO" # Fallback en caso esté suelto
            
        c.drawString(12*mm, y, metodo_nombre)
        c.drawRightString(ancho_ticket - 12*mm, y, f"S/ {float(p_tr.monto):.2f}")
        y -= 4 * mm_unit
        if getattr(p_tr, 'referencia', None):
            c.setFont("Helvetica", 7)
            c.drawString(12*mm, y, f"Ref. : {p_tr.referencia}")
            y -= 4 * mm_unit
            c.setFont("Helvetica", 8)
    total_efectivo_recibido = sum(float(p.monto) for p in pagos_transaccion if p.metodo_pago.codigo == 'EFECTIVO')
    total_vuelto = sum(float(p.vuelto) for p in pagos_transaccion)
    
    if total_vuelto > 0:
        y -= 2 * mm_unit
        c.setFont("Helvetica", 8)
        c.drawRightString(55*mm, y, "Efectivo Recibido")
        c.drawString(58*mm, y, f": S/ {total_efectivo_recibido:.2f}")
        y -= 4 * mm_unit
        c.drawRightString(55*mm, y, "Vuelto")
        c.drawString(58*mm, y, f": S/ {total_vuelto:.2f}")
        y -= 4 * mm_unit
    
    y -= 2 * mm_unit
    c.line(10*mm, y, ancho_ticket - 10*mm, y)
    y -= 4 * mm_unit
    
    # SON: [TEXTO]
    letras = numero_a_letras(monto_total)
    c.setFont("Helvetica-Bold", 7)
    c.drawString(10*mm, y, f"SON: {letras}")
    y -= 3 * mm_unit
    c.line(10*mm, y, ancho_ticket - 10*mm, y)
    y -= 8 * mm_unit
    
    # ─── FOOTER ──────────────────────────────────────────────────────────────
    c.setFont("Helvetica", 7)
    c.drawCentredString(ancho_ticket / 2, y, "Información adicional")
    y -= 4 * mm_unit
    c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(ancho_ticket / 2, y, "¡GRACIAS POR VISITARNOS!")
    y -= 4 * mm_unit
    c.setFont("Helvetica", 7)
    c.drawCentredString(ancho_ticket / 2, y, "Esperamos volver a verte pronto en TICUY.")
    y -= 8 * mm_unit
    
    # QR Code Real
    qr_size = 30 * mm
    qr_x = (ancho_ticket - qr_size) / 2
    try:
        contenido_qr = qr_url if qr_url else f"RES-OS-B001-{pago.id}"
        qr_code = qr.QrCodeWidget(contenido_qr)
        bounds = qr_code.getBounds()
        width = bounds[2] - bounds[0]
        height = bounds[3] - bounds[1]
        d = Drawing(qr_size, qr_size, transform=[qr_size/width, 0, 0, qr_size/height, 0, 0])
        d.add(qr_code)
        from reportlab.graphics import renderPDF
        renderPDF.draw(d, c, qr_x, y - qr_size)
        y -= (qr_size + 5*mm)
    except:
        y -= 10 * mm_unit
    
    c.setFont("Helvetica", 6)
    c.drawCentredString(ancho_ticket / 2, y, "Representación impresa de la BOLETA ELECTRÓNICA.")
    y -= 3 * mm_unit
    c.drawCentredString(ancho_ticket / 2, y, "Autorizado mediante la Resolución N° 034-030-000101/SUNAT")
    
    c.showPage()
    c.save()
    
    buffer.seek(0)
    return buffer
