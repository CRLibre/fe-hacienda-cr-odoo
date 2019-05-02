# -*- coding: utf-8 -*-

import json
import requests
import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import odoo.addons.cr_electronic_invoice.models.functions as functions
import datetime
import pytz
from threading import Lock
lock = Lock()

_logger = logging.getLogger(__name__)

class AccountJournal(models.Model):
    _inherit = 'account.journal'

    payment_method_id = fields.Many2one(comodel_name="payment.methods", string="Payment Methods", required=False, )


class PosConfig(models.Model):
    _inherit = 'pos.config'
    sucursal = fields.Char(string='Point of Sale Sucursal', required=True, help="Sucursal for this point of sale")
    terminal = fields.Char(string='Point of Sale Terminal Number', required=True, help="Terminal number for this point of sale")
    return_sequence_id = fields.Many2one('ir.sequence', string='Order IDs Return Sequence', readonly=False,
        help="This sequence is automatically created by Odoo but you can change it "
        "to customize the reference numbers of your orders.", copy=False)

class PosOrder(models.Model):
    _inherit = "pos.order"

    @api.model
    def sequence_number_sync(self, vals):
        next = vals.get('_sequence_ref_number', False)
        next = int(next) if next else False
        if vals.get('session_id') and next is not False:
            session = self.env['pos.session'].sudo().browse(vals['session_id'])
            if next != session.config_id.sequence_id.number_next_actual:
                session.config_id.sequence_id.number_next_actual = next
        if vals.get('_sequence_ref_number') is not None:
            del vals['_sequence_ref_number']
        if vals.get('_sequence_ref') is not None:
            del vals['_sequence_ref']

    @api.model
    def _order_fields(self, ui_order):
        vals = super(PosOrder, self)._order_fields(ui_order)
        vals['_sequence_ref_number'] = ui_order.get('sequence_ref_number')
        vals['_sequence_ref'] = ui_order.get('sequence_ref')
        return vals

    @api.model
    def create(self, vals):
        real_name = vals.get('_sequence_ref', False)
        if real_name:
            self.sequence_number_sync(vals)
            order = super(PosOrder, self).create(vals)
            _logger.error('MAB - Previous name: %s    New name: %s', order.name, real_name)
            if self.search([('name', '=', real_name)]):
                real_name = self.env['ir.sequence'].next_by_code('pos.order.recovery')
            order.name = real_name
        else:
            if vals.get('pos_order_id', False):
                # set name based on the sequence specified on the config
                session = self.env['pos.session'].browse(vals['session_id'])
                vals['name'] = '/'
                vals.setdefault('pricelist_id', session.config_id.pricelist_id.id)
            order = super(PosOrder, self).create(vals)
        return order

    number_electronic = fields.Char(string="Número electrónico", required=False, copy=False, index=True)
    date_issuance = fields.Char(string="Fecha de emisión", required=False, copy=False)
    state_tributacion = fields.Selection([('aceptado', 'Aceptado'), ('rechazado', 'Rechazado'), ('rejected', 'Rechazado2'),('no_encontrado', 'No encontrado'), ('recibido', 'Recibido'),
         ('error', 'Error'), ('procesando', 'Procesando')], 'Estado FE', copy=False)

    reference_code_id = fields.Many2one(comodel_name="reference.code", string="Código de referencia", required=False)
    pos_order_id = fields.Many2one(comodel_name="pos.order", string="Documento de referencia", required=False, copy=False)
    xml_respuesta_tributacion = fields.Binary(string="Respuesta Tributación XML", required=False, copy=False, attachment=True)
    fname_xml_respuesta_tributacion = fields.Char(string="Nombre de archivo XML Respuesta Tributación", required=False, copy=False)
    xml_comprobante = fields.Binary(string="Comprobante XML", required=False, copy=False, attachment=True)
    fname_xml_comprobante = fields.Char(string="Nombre de archivo Comprobante XML", required=False, copy=False)
    xml_supplier_approval = fields.Binary(string="XML Proveedor", required=False, copy=False, attachment=True)
    fname_xml_supplier_approval = fields.Char(string="Nombre de archivo Comprobante XML proveedor", required=False, copy=False)
    state_email= fields.Selection([('no_email', 'Sin cuenta de correo'), ('sent', 'Enviado'), ('fe_error', 'Error FE')], 'Estado email', copy=False)

    _sql_constraints = [
        ('number_electronic_uniq', 'unique (number_electronic)', "La clave de comprobante debe ser única"),
        ('consecutive_number_receiver_uniq', 'unique (company_id,consecutive_number_receiver)', "Numero de FE repetido, por favor modifique el diario de compras"),
    ]


    @api.multi
    def action_pos_order_paid(self):
        for order in self:
            if order.pos_order_id:
                # set name based on the sequence specified on the config
                order.name = order.session_id.config_id.return_sequence_id._next()
        return super(PosOrder, self).action_pos_order_paid()

    @api.multi
    def refund(self):
        """Create a copy of order  for refund order"""
        PosOrder = self.env['pos.order']
        reference_code_id = self.env['reference.code'].search([('code', '=', '01')], limit=1)
        current_session = self.env['pos.session'].search([('state', '!=', 'closed'),
                                                          ('user_id', '=', self.env.uid),
                                                          ('name', 'not like', 'RESCUE')
                                                          ],
                                                         limit=1)
        if not current_session:
            raise UserError(_('To return product(s), you need to open a session that will be used to register the refund.'))
        for order in self:
            clone = order.copy({
                # ot used, name forced by create
                'name': order.name + _(' REFUND'),
                'session_id': current_session.id,
                'date_order': fields.Datetime.now(),
                'pos_order_id': order.id,
                'reference_code_id': reference_code_id.id,
            })
            PosOrder += clone

        for clone in PosOrder:
            for order_line in clone.lines:
                order_line.write({'qty': -order_line.qty})
        return {
            'name': _('Return Products'),
            'view_type': 'form',
            'view_mode': 'form',
            'res_model': 'pos.order',
            'res_id': PosOrder.ids[0],
            'view_id': False,
            'context': self.env.context,
            'type': 'ir.actions.act_window',
            'target': 'current',
        }

    @api.model
    def _consultahacienda_pos(self, max_orders=10):  #cron
        pos_orders = self.env['pos.order'].search([('state', 'in', ('paid','done','invoiced')),
                                                   ('date_order', '>=', '2018-09-01'),
                                                   ('number_electronic', '!=', False),
                                                   ('state_tributacion', 'in', ('recibido', 'procesando'))],
                                                  limit=max_orders)
        total_orders=len(pos_orders)
        current_order=0
        _logger.error('MAB - Consulta Hacienda - POS Orders to check: %s', total_orders)
        for doc in pos_orders:
            current_order+=1
            _logger.error('MAB - Consulta Hacienda - POS Order %s / %s', current_order, total_orders)
            url = doc.company_id.frm_callback_url


            response_json = functions.token_hacienda(doc, doc.company_id.frm_ws_ambiente, url)

            token_m_h = response_json.get('resp').get('access_token')
            if doc.number_electronic and len(doc.number_electronic) == 50:
                headers = {}
                payload = {}
                payload['w'] = 'consultar'
                payload['r'] = 'consultarCom'
                payload['client_id'] = doc.company_id.frm_ws_ambiente
                payload['token'] = token_m_h
                payload['clave'] = doc.number_electronic
                response = requests.request("POST", url, data=payload, headers=headers)
                responsejson = response.json()
                estado_m_h = responsejson.get('resp').get('ind-estado')
                if estado_m_h == 'aceptado':
                    doc.state_tributacion = estado_m_h
                    doc.fname_xml_respuesta_tributacion = 'AHC_' + doc.number_electronic + '.xml'
                    doc.xml_respuesta_tributacion = responsejson.get('resp').get('respuesta-xml')
                    if doc.partner_id and doc.partner_id.email and not doc.partner_id.opt_out:
                        #email_template = self.env.ref('account.email_template_edi_invoice', False)
                        email_template = self.env.ref(
                            'cr_electronic_invoice_pos.email_template_pos_invoice', False)
                        attachment = self.env['ir.attachment'].search(
                            [('res_model', '=', 'pos.order'), ('res_id', '=', doc.id),
                             ('res_field', '=', 'xml_comprobante')], limit=1)
                        attachment.name = doc.fname_xml_comprobante
                        attachment.datas_fname = doc.fname_xml_comprobante

                        attachment_resp = self.env['ir.attachment'].search(
                            [('res_model', '=', 'pos.order'), ('res_id', '=', doc.id),
                             ('res_field', '=', 'xml_respuesta_tributacion')], limit=1)
                        attachment_resp.name = doc.fname_xml_respuesta_tributacion
                        attachment_resp.datas_fname = doc.fname_xml_respuesta_tributacion

                        email_template.attachment_ids = [(6, 0, [attachment.id, attachment_resp.id])]
                        email_template.with_context(type='binary', default_type='binary').send_mail(doc.id,
                                                                                                    raise_exception=False,
                                                                                                    force_send=True)  # default_type='binary'
                        #                        email_template.attachment_ids = [(3, attachment.id)]
                        #                        email_template.attachment_ids = [(4, attachment_resp.id)]
                        email_template.attachment_ids = [(5)]
                        doc.state_email = 'sent'
                    else:
                        doc.state_email = 'no_email'
                        _logger.info('email no enviado - cliente no definido')
                elif estado_m_h in ('rechazado', 'rejected'):
                    doc.state_tributacion = estado_m_h
                    doc.fname_xml_respuesta_tributacion = 'respuesta_' + doc.number_electronic + '.xml'
                    doc.xml_respuesta_tributacion = responsejson.get('resp').get('respuesta-xml')
                    doc.state_email = 'fe_error'
                    _logger.info('email no enviado - factura rechazada')
                elif estado_m_h == 'error':
                    doc.state_tributacion = estado_m_h
                    doc.state_email = 'fe_error'
                else:
                    doc.state_tributacion = 'no_encontrado'
                    _logger.error('MAB - Consulta Hacienda - POS Order not found: %s', doc.number_electronic)
            else:
                doc.state_tributacion = 'error'
                _logger.error('MAB - POS Order %s  - x Number Electronic: %s formato incorrecto', doc.name, doc.number_electronic)
        _logger.error('MAB - Consulta Hacienda POS- Finalizad Exitosamente')

    @api.model
    def _reenviacorreos_pos(self, max_orders=1):  # cron
        pos_orders = self.env['pos.order'].search([('state', 'in', ('paid','done','invoiced')),
                                                   ('date_order', '>=', '2018-09-01'),
                                                   ('number_electronic', '!=', False),
                                                   ('state_email', '=', False),
                                                   ('state_tributacion', '=', 'aceptado')],
                                                  limit=max_orders
                                                 )
        total_orders=len(pos_orders)
        current_order=0
        _logger.error('MAB - Reenvia Correos- POS Orders to send: %s', total_orders)
        for doc in pos_orders:
            current_order+=1
            _logger.error('MAB - Reenvia Correos- POS Order %s - %s / %s', doc.name, current_order, total_orders)
            if doc.partner_id.email and not doc.partner_id.opt_out and doc.state_tributacion == 'aceptado':
                comprobante = self.env['ir.attachment'].search(
                    [('res_model', '=', 'pos.order'), ('res_id', '=', doc.id),
                     ('res_field', '=', 'xml_comprobante')], limit=1)
                if not comprobante:
                    _logger.info('email no enviado - tiquete sin xml')
                    continue

                try:
                    comprobante.name = doc.fname_xml_comprobante
                except:
                    comprobante.name = 'FE_'+doc.number_electronic+'.xml'

                comprobante.datas_fname = comprobante.name

                respuesta = self.env['ir.attachment'].search(
                    [('res_model', '=', 'pos.order'), ('res_id', '=', doc.id),
                     ('res_field', '=', 'xml_respuesta_tributacion')], limit=1)
                respuesta.name = doc.fname_xml_respuesta_tributacion
                respuesta.datas_fname = doc.fname_xml_respuesta_tributacion

                email_template = self.env.ref(
                    'cr_electronic_invoice_pos.email_template_pos_invoice', False)
                email_template.attachment_ids = [(6, 0, [comprobante.id, respuesta.id])]  # [(4, attachment.id)]
                email_template.with_context(type='binary', default_type='binary').send_mail(doc.id,
                                                                                            raise_exception=False,
                                                                                            force_send=True)  # default_type='binary'
                doc.state_email = 'sent'
            elif doc.state_tributacion in ('rechazado', 'rejected'):
                doc.state_email = 'fe_error'
                _logger.info('email no enviado - factura rechazada')
            else:
                doc.state_email = 'no_email'
                _logger.info('email no enviado - cuenta no definida')
        _logger.error('MAB - Reenvia Correos - Finalizado')

    @api.model
    def _validahacienda_pos(self, max_orders=10):  #cron
        lock.acquire()
        try:
            pos_orders = self.env['pos.order'].search([('state', 'in', ('paid','done','invoiced')),
                                                       #('name', 'like', '506030918%'),
                                                       #('name', 'not like', '**%'),
                                                       ('number_electronic', '=', False),
                                                       ('date_order', '>=', '2018-09-01'),
                                                       #('id', '=', 22145),
                                                       ('state_tributacion', '=', False)],
                                                      order="date_order",
                                                      limit=max_orders)
            total_orders = len(pos_orders)
            current_order = 0
            _logger.error('MAB - Valida Hacienda - POS Orders to check: %s', total_orders)
            for doc in pos_orders:
                current_order += 1
                _logger.error('MAB - Valida Hacienda - POS Order %s / %s', current_order, total_orders)

                docName = doc.name

                if doc.company_id.frm_ws_ambiente != 'disabled' and docName.isdigit():
                    url = doc.company_id.frm_callback_url
                    if doc.amount_total >= 0:
                        tipo_documento = 'FE'
                        tipo_documento_referencia = ''
                        numero_documento_referencia = ''
                        fecha_emision_referencia = ''
                        codigo_referencia = ''
                        razon_referencia = ''
                    else:
                        tipo_documento = 'NC'
                        tipo_documento_referencia = 'FE'
                        numero_documento_referencia = doc.pos_order_id.number_electronic
                        fecha_emision_referencia = doc.pos_order_id.date_issuance
                        codigo_referencia = doc.reference_code_id.code
                        razon_referencia = 'nota credito'
                        #FacturaReferencia = ''   *****************

                    now_utc = datetime.datetime.now(pytz.timezone('UTC'))
                    now_cr = now_utc.astimezone(pytz.timezone('America/Costa_Rica'))
                    dia = docName[3:5]#'%02d' % now_cr.day,
                    mes = docName[5:7]#'%02d' % now_cr.month,
                    anno = docName[7:9]#str(now_cr.year)[2:4],
                    #date_cr = now_cr.strftime("%Y-%m-%dT%H:%M:%S-06:00")
                    date_cr = now_cr.strftime("20"+anno+"-"+mes+"-"+dia+"T%H:%M:%S-06:00")
                    #date_cr = now_cr.strftime("2018-09-01T07:25:32-06:00")
                    codigo_seguridad = docName[-8:]  # ,doc.company_id.security_code,
                    if not doc.statement_ids[0].statement_id.journal_id.payment_method_id:
                        _logger.error('MAB 001 - codigo seguridad : %s  -- Pedido: %s Metodo de pago de diario no definido, utilizando efectivo', codigo_seguridad, docName)
                    medio_pago = doc.statement_ids[0].statement_id.journal_id.payment_method_id and doc.statement_ids[0].statement_id.journal_id.payment_method_id.sequence or '01'
                    sale_conditions = '01' #Contado !!   doc.sale_conditions_id.sequence,

                    currency_rate = 1 # 1 / doc.currency_id.rate
                    lines = []
                    numero = 0
                    vat=doc.partner_id.vat

                    if doc.partner_id and doc.partner_id.vat: #and doc.partner_id.email:
                        if not doc.partner_id.identification_id:
                            if len(doc.partner_id.vat)==9:  #cedula fisica
                                id_code='01'
                            elif len(doc.partner_id.vat)==10:  #cedula juridica
                                id_code='02'
                            elif len(doc.partner_id.vat)==11 or len(doc.partner_id.vat)==12:  #dimex
                                id_code = '03'
                            else:
                                id_code = '05'
                        else:
                            id_code = doc.partner_id.identification_id.code

                        if id_code == '05':
                            receptor_identificacion = {
                                'tipo': False,
                                'numero': False,
                            }
                            receptor_identificacion_extranjero = doc.partner_id.vat
                        else:
                            receptor_identificacion = {
                                'tipo': id_code,
                                'numero': doc.partner_id.vat,
                            }
                            receptor_identificacion_extranjero =''
                        receptor = {##
                                'nombre': doc.partner_id.name[:80],
                                'identificacion': receptor_identificacion,
                                'IdentificacionExtranjero': receptor_identificacion_extranjero,
                            }
                    else:
                        receptor = ''

                    # Generamos las líneas de la factura
                    lines = dict()
                    line_number = 0
                    total_servicio_gravado = 0.0
                    total_servicio_exento = 0.0
                    total_mercaderia_gravado = 0.0
                    total_mercaderia_exento = 0.0
                    total_descuento = 0.0
                    total_impuestos = 0.0
                    base_subtotal = 0.0

                    for line in doc.lines:
                        #impuestos_acumulados = 0.0
                        numero +=1
                        price = line.price_unit * (1 - line.discount / 100.0)
                        qty = abs(line.qty)
                        if not qty:
                            continue
                        fpos = line.order_id.fiscal_position_id
                        tax_ids = fpos.map_tax(line.tax_ids, line.product_id, line.order_id.partner_id) if fpos else line.tax_ids
                        line_taxes = tax_ids.compute_all(price, line.order_id.pricelist_id.currency_id, 1, product=line.product_id, partner=line.order_id.partner_id)

                        price_unit = round(line_taxes['total_excluded'] / (1 - line.discount / 100.0), 5)  #ajustar para IVI
                        base_line = abs(round(price_unit * qty, 5))
                        subtotal_line = abs(round(price_unit * qty * (1 - line.discount / 100.0), 5))

                        dline = {
                            "cantidad": qty,
                            "unidadMedida": line.product_id and line.product_id.uom_id.code or 'Sp',
                            "detalle": line.name[:159],
                            "precioUnitario": price_unit,
                            "montoTotal": base_line,
                            "subtotal": subtotal_line,
                        }
                        if line.discount:
                            descuento = abs(base_line - subtotal_line)
                            total_descuento += descuento
                            line["montoDescuento"] = descuento
                            line["naturalezaDescuento"] = 'Descuento Comercial'

                        # Se generan los impuestos
                        taxes = dict()
                        impuesto_linea = 0.0
                        if tax_ids:
                            tax_index = 0

                            taxes_lookup = {}
                            for i in tax_ids:
                                taxes_lookup[i.id] = {'tax_code': i.tax_code, 'tarifa': i.amount}
                            for i in line_taxes['taxes']:
                                if taxes_lookup[i['id']]['tax_code'] != '00':
                                    tax_index += 1
                                    tax_amount = abs(round(i['amount'], 5) * qty)
                                    impuesto_linea += tax_amount
                                    taxes[tax_index] = {
                                        'codigo': taxes_lookup[i['id']]['tax_code'],
                                        'tarifa': taxes_lookup[i['id']]['tarifa'],
                                        'monto': tax_amount,
                                    }

                        dline["impuesto"] = taxes

                        # Si no hay product_id se asume como mercaderia
                        if line.product_id and line.product_id.type == 'service':
                            if taxes:
                                total_servicio_gravado += base_line
                                total_impuestos += impuesto_linea
                            else:
                                total_servicio_exento += base_line
                        else:
                            if taxes:
                                total_mercaderia_gravado += base_line
                                total_impuestos += impuesto_linea
                            else:
                                total_mercaderia_exento += base_line

                        base_subtotal += subtotal_line

                        dline["montoTotalLinea"] = subtotal_line + impuesto_linea

                        lines[line_number] = dline
                    consecutivo = docName[21:41]
                    doc.number_electronic = docName
                    response_json = functions.make_xml_invoice(doc, 'TE', consecutivo, date_cr,
                                                               sale_conditions, medio_pago,
                                                               total_servicio_gravado,
                                                               total_servicio_exento, total_mercaderia_gravado,
                                                               total_mercaderia_exento, base_subtotal,
                                                               total_impuestos, total_descuento,
                                                               json.dumps(lines, ensure_ascii=False),
                                                               tipo_documento_referencia,
                                                               numero_documento_referencia,
                                                               fecha_emision_referencia,
                                                               codigo_referencia, razon_referencia, url,
                                                               currency_rate)

                    _logger.error('MAB - unsigned JSON DATA:%s', response_json)
                    xml = response_json.get('resp').get('xml')
                    response_json = functions.sign_xml(doc, tipo_documento, url, xml)
                    xml_firmado = response_json.get('resp').get('xmlFirmado')
                    _logger.error('MAB - SIGNED XML:%s', xml_firmado)

                    # get token
                    # url param added by @jbarboza
                    response_json = functions.token_hacienda(doc, doc.company_id.frm_ws_ambiente, url)
                    token_m_h = response_json.get('resp').get('access_token')

                    _logger.error('MAB 002 - enviando documento')
                    response_json = functions.send_file(doc, token_m_h, date_cr, xml_firmado,
                                                        doc.company_id.frm_ws_ambiente, url)

                    _logger.error('MAB 003 - respuesta recibida')
                    if response_json.get('resp').get('Status') == 202:
                        #functions.consulta_documentos(self, doc, 'TE', doc.company_id.frm_ws_ambiente, token_m_h, url,
                        #                              date_cr, xml_firmado)
                        doc.state_tributacion = 'procesando'
                        doc.date_issuance = date_cr
                        doc.fname_xml_comprobante = 'comprobante_' + doc.number_electronic + '.xml'
                        doc.xml_comprobante = xml_firmado
                    else:
                        _logger.error(
                            'No se pudo Crear la factura electrónica: \n' + str(
                                response_json.get('resp').get('text')))
                        number_electronic = '-1--' + docName + 'text :' + str(
                                response_json.get('resp').get('text'))
                else:
                    _logger.error('MAB 013 - Pos Order:%s skipped.  FE disabled', doc.name)
            _logger.error('MAB 014 - Valida Hacienda POS- Finalizado Exitosamente')
        finally:
            lock.release()

