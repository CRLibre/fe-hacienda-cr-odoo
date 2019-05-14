# -*- coding: utf-8 -*-
import requests
import logging
import re
import datetime
import pytz
import base64
import json
from dateutil.parser import parse
from xml.sax.saxutils import escape
from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.tools.safe_eval import safe_eval
from . import functions
from lxml import etree

_logger = logging.getLogger(__name__)


class AccountInvoiceRefund(models.TransientModel):
    _inherit = "account.invoice.refund"

    @api.model
    def _get_invoice_id(self):
        context = dict(self._context or {})
        active_id = context.get('active_id', False)
        if active_id:
            return active_id
        return ''

    reference_code_id = fields.Many2one(comodel_name="reference.code", string="Código de referencia", required=True,)
    invoice_id = fields.Many2one(comodel_name="account.invoice", string="Documento de referencia",
                                 default=_get_invoice_id, required=False,)

    @api.multi
    def compute_refund(self, mode='refund'):
        if self.env.user.company_id.frm_ws_ambiente == 'disabled':
            result = super(AccountInvoiceRefund, self).compute_refund()
            return result
        else:
            inv_obj = self.env['account.invoice']
            inv_tax_obj = self.env['account.invoice.tax']
            inv_line_obj = self.env['account.invoice.line']
            context = dict(self._context or {})
            xml_id = False

            for form in self:
                created_inv = []
                for inv in inv_obj.browse(context.get('active_ids')):
                    if inv.state in ['draft', 'proforma2', 'cancel']:
                        raise UserError(_('Cannot refund draft/proforma/cancelled invoice.'))
                    if inv.reconciled and mode in ('cancel', 'modify'):
                        raise UserError(_('Cannot refund invoice which is already reconciled, invoice should be '
                                          'unreconciled first. You can only refund this invoice.'))

                    date = form.date or False
                    description = form.description or inv.name
                    refund = inv.refund(form.date_invoice, date, description, inv.journal_id.id, form.invoice_id.id,
                                        form.reference_code_id.id)

                    created_inv.append(refund.id)
                    if mode in ('cancel', 'modify'):
                        movelines = inv.move_id.line_ids
                        to_reconcile_ids = {}
                        to_reconcile_lines = self.env['account.move.line']
                        for line in movelines:
                            if line.account_id.id == inv.account_id.id:
                                to_reconcile_lines += line
                                to_reconcile_ids.setdefault(line.account_id.id, []).append(line.id)
                            if line.reconciled:
                                line.remove_move_reconcile()
                        refund.action_invoice_open()
                        for tmpline in refund.move_id.line_ids:
                            if tmpline.account_id.id == inv.account_id.id:
                                to_reconcile_lines += tmpline
                        to_reconcile_lines.filtered(lambda l: l.reconciled == False).reconcile()
                        if mode == 'modify':
                            invoice = inv.read(inv_obj._get_refund_modify_read_fields())
                            invoice = invoice[0]
                            del invoice['id']
                            invoice_lines = inv_line_obj.browse(invoice['invoice_line_ids'])
                            invoice_lines = inv_obj.with_context(mode='modify')._refund_cleanup_lines(invoice_lines)
                            tax_lines = inv_tax_obj.browse(invoice['tax_line_ids'])
                            tax_lines = inv_obj._refund_cleanup_lines(tax_lines)
                            invoice.update({
                                'type': inv.type,
                                'date_invoice': form.date_invoice,
                                'state': 'draft',
                                'number': False,
                                'invoice_line_ids': invoice_lines,
                                'tax_line_ids': tax_lines,
                                'date': date,
                                'origin': inv.origin,
                                'fiscal_position_id': inv.fiscal_position_id.id,
                                'invoice_id': inv.id,  # agregado
                                'reference_code_id': form.reference_code_id.id,  # agregado
                            })
                            for field in inv_obj._get_refund_common_fields():
                                if inv_obj._fields[field].type == 'many2one':
                                    invoice[field] = invoice[field] and invoice[field][0]
                                else:
                                    invoice[field] = invoice[field] or False
                            inv_refund = inv_obj.create(invoice)
                            if inv_refund.payment_term_id.id:
                                inv_refund._onchange_payment_term_date_invoice()
                            created_inv.append(inv_refund.id)
                    xml_id = (inv.type in ['out_refund', 'out_invoice']) and 'action_invoice_tree1' or \
                             (inv.type in ['in_refund', 'in_invoice']) and 'action_invoice_tree2'
                    # Put the reason in the chatter
                    subject = _("Invoice refund")
                    body = description
                    refund.message_post(body=body, subject=subject)
            if xml_id:
                result = self.env.ref('account.%s' % (xml_id)).read()[0]
                invoice_domain = safe_eval(result['domain'])
                invoice_domain.append(('id', 'in', created_inv))
                result['domain'] = invoice_domain
                return result
            return True


class InvoiceLineElectronic(models.Model):
    _inherit = "account.invoice.line"

    exoneration_id = fields.Many2one(comodel_name="exoneration", string="Exoneración", required=False,)


class AccountInvoiceElectronic(models.Model):
    _inherit = "account.invoice"

    number_electronic = fields.Char(string="Número electrónico", required=False, copy=False, index=True)
    date_issuance = fields.Char(string="Fecha de emisión", required=False, copy=False)
    consecutive_number_receiver = fields.Char(string="Número Consecutivo Receptor", required=False, copy=False,
                                              readonly=True, index=True)
    state_send_invoice = fields.Selection([('aceptado', 'Aceptado'),
                                           ('rechazado', 'Rechazado'),
                                           ('error', 'Error'),
                                           ('na', 'No Aplica'),
                                           ('ne', 'No Encontrado'),
                                           ('firma_invalida', 'Firma Inválida'),
                                           ('procesando', 'Procesando')],
                                          'Estado FE Proveedor')
    state_tributacion = fields.Selection(
        [('aceptado', 'Aceptado'), ('rechazado', 'Rechazado'), ('recibido', 'Recibido'),
         ('firma_invalida', 'Firma Inválida'),
         ('error', 'Error'), ('procesando', 'Procesando'), ('na', 'No Aplica'), ('ne', 'No Encontrado')], 'Estado FE',
        copy=False)
    state_invoice_partner = fields.Selection([('1', 'Aceptado'), ('3', 'Rechazado'), ('2', 'Aceptacion parcial')],
                                             'Respuesta del Cliente')
    reference_code_id = fields.Many2one(comodel_name="reference.code", string="Código de referencia", required=False,)
    payment_methods_id = fields.Many2one(comodel_name="payment.methods", string="Métodos de Pago", required=False,)
    invoice_id = fields.Many2one(comodel_name="account.invoice", string="Documento de referencia", required=False,
                                 copy=False)
    xml_respuesta_tributacion = fields.Binary(string="Respuesta Tributación XML", required=False, copy=False,
                                              attachment=True)
    fname_xml_respuesta_tributacion = fields.Char(string="Nombre de archivo XML Respuesta Tributación", required=False,
                                                  copy=False)
    xml_comprobante = fields.Binary(string="Comprobante XML", required=False, copy=False, attachment=True)
    fname_xml_comprobante = fields.Char(string="Nombre de archivo Comprobante XML", required=False, copy=False,
                                        attachment=True)
    xml_supplier_approval = fields.Binary(string="XML Proveedor", required=False, copy=False, attachment=True)
    fname_xml_supplier_approval = fields.Char(string="Nombre de archivo Comprobante XML proveedor", required=False,
                                              copy=False, attachment=True)
    amount_tax_electronic_invoice = fields.Monetary(string='Total de impuestos FE', readonly=True,)
    amount_total_electronic_invoice = fields.Monetary(string='Total FE', readonly=True,)
    state_email = fields.Selection([('no_email', 'Sin cuenta de correo'), ('sent', 'Enviado'),
                                    ('fe_error', 'Error FE')], 'Estado email', copy=False)

    ignore_total_difference = fields.Boolean(string="Ingorar Diferencia en Totales", required=False, default=False)
    error_count = fields.Integer(string="Cantidad de errores", required=False, default="0")
    _sql_constraints = [
        ('number_electronic_uniq', 'unique (company_id, number_electronic)', "La clave de comprobante debe ser única"),
    ]

    @api.onchange('xml_supplier_approval')
    def _onchange_xml_supplier_approval(self):
        if self.xml_supplier_approval:
            xml_decoded = base64.b64decode(self.xml_supplier_approval)
            try:
                factura = etree.fromstring(xml_decoded)
            except Exception, e:
                # raise UserError(_(
                #    "This XML file is not XML-compliant. Error: %s") % e)
                _logger.info('MAB - This XML file is not XML-compliant.  Exception %s' % e)
                return {'status': 400, 'text': 'Excepción de conversión de XML'}
            pretty_xml_string = etree.tostring(
                factura, pretty_print=True, encoding='UTF-8',
                xml_declaration=True)


            namespaces = factura.nsmap
            inv_xmlns = namespaces.pop(None)
            namespaces['inv'] = inv_xmlns

            if not factura.xpath("inv:Clave", namespaces=namespaces):
                return {'value': {'xml_supplier_approval': False}, 'warning': {'title': 'Atención',
                                                                               'message': 'El archivo xml no contiene el nodo Clave. Por favor cargue un archivo con el formato correcto.'}}
            if not factura.xpath("inv:FechaEmision", namespaces=namespaces):
                return {'value': {'xml_supplier_approval': False}, 'warning': {'title': 'Atención',
                                                                               'message': 'El archivo xml no contiene el nodo FechaEmision. Por favor cargue un archivo con el formato correcto.'}}
            if not factura.xpath("inv:Emisor/inv:Identificacion/inv:Numero", namespaces=namespaces):
                return {'value': {'xml_supplier_approval': False}, 'warning': {'title': 'Atención',
                                                                               'message': 'El archivo xml no contiene el nodo Emisor. Por favor cargue un archivo con el formato correcto.'}}
            if not factura.xpath("inv:ResumenFactura/inv:TotalComprobante", namespaces=namespaces):
                return {'value': {'xml_supplier_approval': False}, 'warning': {'title': 'Atención',
                                                                               'message': 'No se puede localizar el nodo TotalComprobante. Por favor cargue un archivo con el formato correcto.'}}
        else:
            self.state_tributacion = False
            self.state_send_invoice = False
            self.xml_supplier_approval = False
            self.fname_xml_supplier_approval = False
            self.xml_respuesta_tributacion = False
            self.fname_xml_respuesta_tributacion = False
            self.date_issuance = False
            self.number_electronic = False
            self.state_invoice_partner = False

    @api.multi
    def charge_xml_data(self):
        if self.xml_supplier_approval:
            xml_decoded = base64.b64decode(self.xml_supplier_approval)
            try:
                factura = etree.fromstring(xml_decoded)
            except Exception as e:
                #doble decode para los casos en que el proveedor envía el archivo en base64
                self.xml_supplier_approval = xml_decoded
                xml_decoded = base64.b64decode(self.xml_supplier_approval)
                try:
                    factura = etree.fromstring(xml_decoded)
                except Exception as e:
                    _logger.info('MAB - This XML file is not XML-compliant.  Exception %s' % e)
                    return {'status': 400, 'text': 'Excepción de conversión de XML'}
            pretty_xml_string = etree.tostring(
                factura, pretty_print=True, encoding='UTF-8',
                xml_declaration=True)

            _logger.error('MAB - charge_xml_data XML: %s' % pretty_xml_string)

            namespaces = factura.nsmap
            inv_xmlns = namespaces.pop(None)
            namespaces['inv'] = inv_xmlns

            number_electronic_node = factura.xpath("inv:Clave", namespaces=namespaces)
            date_issuance_node = factura.xpath("inv:FechaEmision", namespaces=namespaces)
            emisor_node = factura.xpath("inv:Emisor/inv:Identificacion/inv:Numero", namespaces=namespaces)
            receptor_node = factura.xpath("inv:Receptor/inv:Identificacion/inv:Numero", namespaces=namespaces)

            if not number_electronic_node:
                self.state_send_invoice = 'error'
                self.message_post(subject='Error', body=u'El archivo XML no tiene clave numérica!')
                return
            elif not date_issuance_node:
                self.state_send_invoice = 'error'
                self.message_post(subject='Error', body=u'El archivo XML no tiene fecha de emisión!')
                return
            elif not emisor_node:
                self.state_send_invoice = 'error'
                self.message_post(subject='Error', body=u'El archivo XML no tiene emisor!')
                return
            elif not receptor_node:
                self.state_send_invoice = 'error'
                self.message_post(subject='Error', body=u'El archivo XML no tiene receptor!')
                return

            self.number_electronic = number_electronic_node[0].text
            self.date_issuance = date_issuance_node[0].text
            emisor = emisor_node[0].text
            receptor = receptor_node[0].text

            if receptor != self.company_id.vat:
                self.state_send_invoice = 'error'
                self.message_post(subject='Error', body=u'El receptor no corresponde con la compañía actual con identificación ' + receptor + u'. Por favor active la compañía correcta.')
                return

            self.date_invoice = parse(self.date_issuance)

            partner = self.env['res.partner'].search(
                [('vat', '=', emisor),
                 ('supplier', '=', True),
                 '|', ('company_id', '=', self.company_id.id), ('company_id', '=', False)], limit=1)
            if partner:
                self.partner_id = partner.id
            else:
                self.state_send_invoice = 'error'
                self.message_post(subject='Error', body=u'El proveedor con identificación ' + emisor + u' no existe. Por favor creelo primero en el sistema.')
                return

            self.reference = self.number_electronic[21:41]
            tax_node = factura.xpath("inv:ResumenFactura/inv:TotalImpuesto", namespaces=namespaces)
            if tax_node:
                self.amount_tax_electronic_invoice = tax_node[0].text

            self.amount_total_electronic_invoice = factura.xpath("inv:ResumenFactura/inv:TotalComprobante",
                                                                 namespaces=namespaces)[0].text

    @api.multi
    def send_acceptance_message(self):
        for inv in self:
            if inv.state_send_invoice and inv.state_send_invoice in ('aceptado', 'rechazado', 'na'):
                inv.state_send_invoice = 'error'
                inv.message_post(subject='Error', body='Aviso!.\n La factura de proveedor ya fue confirmada')
                continue
            elif not self.ignore_total_difference and abs(self.amount_total_electronic_invoice-self.amount_total) > 1:
                inv.state_send_invoice = 'error'
                inv.message_post(subject='Error', body='Aviso!.\n Monto total no concuerda con monto del XML')
                continue
                #raise UserError('Aviso!.\n Monto total no concuerda con monto del XML')
            elif not inv.xml_supplier_approval:
                inv.state_send_invoice = 'error'
                inv.message_post(subject='Error', body='Aviso!.\n No se ha cargado archivo XML')
                continue
                #raise UserError('Aviso!.\n No se ha cargado archivo XML')
            elif not inv.journal_id.sucursal or not inv.journal_id.terminal:
                inv.state_send_invoice = 'error'
                inv.message_post(subject='Error', body='Aviso!.\nPor favor configure el diario de compras, terminal y sucursal')
                continue

            url = self.company_id.frm_callback_url
            if not inv.state_invoice_partner:
                inv.state_send_invoice = 'error'
                inv.message_post(subject='Error', body='Aviso!.\nDebe primero seleccionar el tipo de respuesta para el archivo cargado.')
                continue
                #raise UserError('Aviso!.\nDebe primero seleccionar el tipo de respuesta para el archivo cargado.')

            if inv.company_id.frm_ws_ambiente != 'disabled' and inv.state_invoice_partner:
                now_utc = datetime.datetime.now(pytz.timezone('UTC'))
                now_cr = now_utc.astimezone(pytz.timezone('America/Costa_Rica'))
                date_cr = now_cr.strftime("%Y-%m-%dT%H:%M:%S-06:00")
                if not inv.xml_comprobante or inv.state_send_invoice == 'rechazado':
                    if inv.state_invoice_partner == '1':
                        detalle_mensaje = 'Aceptado'
                        tipo = 1
                        tipo_documento = 'CCE'
                        sequence = inv.env['ir.sequence'].next_by_code('sequece.electronic.doc.confirmation')
                    elif inv.state_invoice_partner == '2':
                        detalle_mensaje = 'Aceptado parcial'
                        tipo = 2
                        tipo_documento = 'CPCE'
                        sequence = inv.env['ir.sequence'].next_by_code('sequece.electronic.doc.partial.confirmation')
                    else:
                        detalle_mensaje = 'Rechazado'
                        tipo = 3
                        tipo_documento = 'RCE'
                        sequence = inv.env['ir.sequence'].next_by_code('sequece.electronic.doc.reject')

                    '''Si el mensaje fue rechazado, necesitamos generar un nuevo id'''
                    if inv.state_send_invoice == 'rechazado':
                        message_description += '<p><b>Cambiando consecutivo del Mensaje de Receptor</b> <br />' \
                                               '<b>Consecutivo anterior: </b>' + inv.consecutive_number_receiver + \
                                               '<br/>' \
                                              '<b>Estado anterior: </b>' + inv.state_send_invoice + '</p>'

                    payload = {}
                    headers = {}

                    # usamos un consecutivo único por tipo de confirmación/rechazo para TODA la empresa
                    #response_json = functions.get_clave(self, url, tipo_documento, sequence, inv.journal_id.sucursal, inv.journal_id.terminal)
                    response_json = functions.get_clave(self, tipo_documento, sequence, 1, 1)
                    inv.consecutive_number_receiver = response_json.get('consecutivo')

                    response_json = functions.make_msj_receptor(url, inv.number_electronic, inv.partner_id.vat,
                                                      inv.date_issuance, tipo, detalle_mensaje,
                                                      inv.company_id.vat, inv.consecutive_number_receiver,
                                                      inv.amount_tax_electronic_invoice,
                                                      inv.amount_total_electronic_invoice)

                     ##1= response.json()
                    if response_json['status'] != 200:
                        _logger.error('MAB - API Error creating XML:%s', response_json['text'])
                        inv.state_tributacion = 'error'
                        continue

                    xml = response_json.get('xml')

                    response_json = functions.sign_xml(inv, tipo_documento, url, xml)
                    if response_json['status'] != 200:
                        _logger.error('MAB - API Error signing XML:%s', response_json['text'])
                        inv.state_send_invoice = 'error'
                        inv.message_post(subject='Error',
                                         body='API Error signing XML:' + response_json['text'])
                        continue

                    xml_firmado = response_json.get('xmlFirmado')

                    inv.fname_xml_comprobante = tipo_documento + '_' + inv.number_electronic + '.xml'
                    inv.xml_comprobante = xml_firmado
                    #inv.date_issuance = date_cr
                    _logger.error('MAB - SIGNED XML:%s', inv.fname_xml_comprobante)

                if inv.state_send_invoice != 'procesando':
                    env = inv.company_id.frm_ws_ambiente
                    response_json = functions.token_hacienda(inv.company_id)
                    if response_json['status'] != 200:
                        _logger.error('MAB - Send Acceptance Message - HALTED - Failed to get token')
                        return

                    response_json = functions.send_message(inv, date_cr, response_json['token'],  env)

                    response_status = response_json.get('status')
                    response_text = response_json.get('text')
                    if 200 <=  status <= 299:
                        inv.state_send_invoice = 'procesando'
                        #functions.consulta_documentos(self, inv, env, token_m_h, url, date_cr, xml_firmado)
                    else:
                        if response_text.find('ya fue recibido anteriormente') != -1:
                            inv.state_send_invoice = 'procesando'
                            inv.message_post(subject='Error', body='Ya recibido anteriormente, se pasa a consultar')
                        else:
                            inv.state_send_invoice = 'error'
                            _logger.error('MAB - Invoice: %s  Error sending Acceptance Message: %s', inv.number_electronic,
                                          response_text)

                if inv.state_send_invoice == 'procesando':
                    response_json = functions.token_hacienda(inv.company_id)
                    if response_json['status'] != 200:
                        _logger.error('MAB - Send Acceptance Message - HALTED - Failed to get token')
                        return

                    response_json = functions.consulta_clave(inv.number_electronic+'-'+inv.consecutive_number_receiver,
                                                             response_json['token'],
                                                             inv.company_id.frm_ws_ambiente)
                    status = response_json['status']
                    if status == 200:
                        inv.state_send_invoice = response_json.get('ind-estado')
                        inv.xml_respuesta_tributacion = response_json.get('respuesta-xml')
                        inv.fname_xml_respuesta_tributacion = 'Aceptacion_' + inv.number_electronic+'-'+inv.consecutive_number_receiver + '.xml'
                        _logger.error('MAB - Estado Documento:%s', inv.state_send_invoice)
                    elif status == 400:
                        inv.state_send_invoice = 'ne'
                        _logger.error('MAB - Aceptacion Documento:%s no encontrado en Hacienda.',
                                      inv.number_electronic+'-'+inv.consecutive_number_receiver)
                    else:
                        _logger.error('MAB - Error inesperado en Send Acceptance File - Abortando')
                        return


    @api.multi
    @api.returns('self')
    def refund(self, date_invoice=None, date=None, description=None, journal_id=None, invoice_id=None,
               reference_code_id=None):
        if self.env.user.company_id.frm_ws_ambiente == 'disabled':
            new_invoices = super(AccountInvoiceElectronic, self).refund()
            return new_invoices
        else:
            new_invoices = self.browse()
            for invoice in self:
                # create the new invoice
                values = self._prepare_refund(invoice, date_invoice=date_invoice, date=date, description=description,
                                              journal_id=journal_id)

                values.update({'invoice_id': invoice_id, 'reference_code_id': reference_code_id})
                refund_invoice = self.create(values)
                invoice_type = {
                    'out_invoice': ('customer invoices refund'),
                    'in_invoice': ('vendor bill refund'),
                    'out_refund': ('customer refund refund'),
                    'in_refund': ('vendor refund refund')
                }
                message = _("This %s has been created from: <a href=# data-oe-model=account.invoice data-oe-id=%d>%s</a>") % (
                              invoice_type[invoice.type], invoice.id, invoice.number)
                refund_invoice.message_post(body=message)
                refund_invoice.payment_methods_id = invoice.payment_methods_id
                refund_invoice.payment_term_id = invoice.payment_term_id
                new_invoices += refund_invoice
            return new_invoices

    @api.onchange('partner_id', 'company_id')
    def _onchange_partner_id(self):
        super(AccountInvoiceElectronic, self)._onchange_partner_id()
        self.payment_methods_id = self.partner_id.payment_methods_id

    @api.model
    def _consultahacienda(self, max_invoices=10):  # cron
        invoices = self.env['account.invoice'].search([('type', 'in', ('out_invoice', 'out_refund')),
                                                       ('state', 'in', ('open', 'paid')),
                                                       ('state_tributacion', 'in', ('recibido', 'procesando', 'ne'))],
                                                      limit=max_invoices)
        total_invoices = len(invoices)
        current_invoice = 0
        _logger.info('MAB - Consulta Hacienda - Facturas a Verificar: %s', total_invoices)

        for i in invoices:
            current_invoice += 1
            _logger.info('MAB - Consulta Hacienda - Invoice %s / %s  -  number:%s', current_invoice,
                         total_invoices, i.number_electronic)

            response_json = functions.token_hacienda(i.company_id)
            if response_json['status'] != 200:
                _logger.error('MAB - Consulta Hacienda - HALTED - Failed to get token')
                return

            if i.number_electronic and len(i.number_electronic) == 50:
                response_json = functions.consulta_clave(i.number_electronic, response_json['token'], 
                                                            i.company_id.frm_ws_ambiente)
                status = response_json['status']

                if status == 200:
                    estado_m_h = response_json.get('ind-estado')
                    _logger.info('MAB - Estado Documento:%s', estado_m_h)
                elif status == 400:
                    estado_m_h = response_json.get('ind-estado')
                    i.state_tributacion = 'ne'
                    _logger.warning('MAB - Documento:%s no encontrado en Hacienda.  Estado: %s', i.number_electronic,
                                    estado_m_h)
                    continue
                else:
                    _logger.error('MAB - Error inesperado en Consulta Hacienda - Abortando')
                    return

                i.state_tributacion = estado_m_h
                if estado_m_h == 'aceptado':
                    i.fname_xml_respuesta_tributacion = 'AHC_' + i.number_electronic + '.xml'
                    i.xml_respuesta_tributacion = response_json.get('respuesta-xml')
                    if i.partner_id and i.partner_id.email and not i.partner_id.opt_out and (not i.invoice_id or i.invoice_id.state_tributacion == 'aceptado'):
                        email_template = self.env.ref('account.email_template_edi_invoice', False)
                        attachment = self.env['ir.attachment'].search(
                            [('res_model', '=', 'account.invoice'), ('res_id', '=', i.id),
                             ('res_field', '=', 'xml_comprobante')], limit=1)
                        attachment.name = i.fname_xml_comprobante
                        attachment.datas_fname = i.fname_xml_comprobante

                        attachment_resp = self.env['ir.attachment'].search(
                            [('res_model', '=', 'account.invoice'), ('res_id', '=', i.id),
                             ('res_field', '=', 'xml_respuesta_tributacion')], limit=1)
                        attachment_resp.name = i.fname_xml_respuesta_tributacion
                        attachment_resp.datas_fname = i.fname_xml_respuesta_tributacion

                        email_template.attachment_ids = [(6, 0, [attachment.id, attachment_resp.id])]

                        email_template.with_context(type='binary', default_type='binary').send_mail(
                            i.id, raise_exception=False, force_send=True)

                        email_template.attachment_ids = [(5)]

                elif estado_m_h in ('firma_invalida'):
                    if i.error_count > 10:
                        i.fname_xml_respuesta_tributacion = 'AHC_' + i.number_electronic + '.xml'
                        i.xml_respuesta_tributacion = response_json.get('respuesta-xml')
                        i.state_email = 'fe_error'
                        _logger.info('email no enviado - factura rechazada')
                    else:
                        i.error_count += 1
                        i.state_tributacion = 'procesando'

                elif estado_m_h == 'rechazado':
                    i.state_email = 'fe_error'
                    i.fname_xml_respuesta_tributacion = 'AHC_' + i.number_electronic + '.xml'
                    i.xml_respuesta_tributacion = response_json.get('respuesta-xml')
                else:
                    if i.error_count > 10:
                        i.state_tributacion = 'error'
                    elif i.error_count < 4:
                        i.error_count += 1
                        i.state_tributacion = 'procesando'
                    else:
                        i.error_count += 1
                        i.state_tributacion = ''
                    #doc.state_tributacion = 'no_encontrado'
                    _logger.error('MAB - Consulta Hacienda - Invoice not found: %s  -  Estado Hacienda: %s', i.number_electronic, estado_m_h)

    @api.multi
    def action_consultar_hacienda(self):
        if self.company_id.frm_ws_ambiente != 'disabled':
            for inv in self:
                response_json = functions.token_hacienda(inv.company_id)
                functions.consulta_documentos(self, inv, self.company_id.frm_ws_ambiente, response_json['token'],
                                              self.company_id.frm_callback_url, False, False)

    @api.model
    def _confirmahacienda(self, max_invoices=10):  # cron
        invoices = self.env['account.invoice'].search([('type', 'in', ('in_invoice', 'in_refund')),
                                                       ('state', 'in', ('open', 'paid')),
                                                       ('xml_supplier_approval', '!=', False),
                                                       ('state_invoice_partner', '!=', False),
                                                       ('state_send_invoice', 'not in', ('aceptado', 'rechazado',
                                                                                         'error', 'na'))],
                                                      limit=max_invoices)
        total_invoices=len(invoices)
        current_invoice=0
        _logger.info('MAB - Confirma Hacienda - Invoices to check: %s', total_invoices)
        for i in invoices:
            current_invoice+=1
            #if abs(i.amount_total_electronic_invoice - i.amount_total) > 1:
            if not i.amount_total_electronic_invoice:
                #continue   # xml de proveedor no se ha procesado, debemos llamar la carga
                i.charge_xml_data()
            _logger.info('MAB - Confirma Hacienda - Invoice %s / %s  -  number:%s', current_invoice, total_invoices, i.number_electronic)

            i.send_acceptance_message()

            if i.state_send_invoice == 'aceptado':
                continue  # hay que revisar rutina de envio de email, por eso se interrumpe aca
                if i.partner_id and i.partner_id.email and not i.partner_id.opt_out:
                    email_template = self.env.ref('cr_electronic_invoice.email_template_invoice_vendor', False)
                    attachment = self.env['ir.attachment'].search(
                        [('res_model', '=', 'account.invoice'), ('res_id', '=', i.id),
                         ('res_field', '=', 'xml_comprobante')], limit=1)
                    attachment.name = i.fname_xml_comprobante
                    attachment.datas_fname = i.fname_xml_comprobante

                    attachment_resp = self.env['ir.attachment'].search(
                        [('res_model', '=', 'account.invoice'), ('res_id', '=', i.id),
                         ('res_field', '=', 'xml_respuesta_tributacion')], limit=1)
                    attachment_resp.name = i.fname_xml_respuesta_tributacion
                    attachment_resp.datas_fname = i.fname_xml_respuesta_tributacion

                    email_template.attachment_ids = [(6, 0, [attachment.id, attachment_resp.id])]
                    email_template.with_context(type='binary', default_type='binary').send_mail(i.id,
                                                                                                raise_exception=False,
                                                                                                force_send=True)  # default_type='binary'
                    email_template.attachment_ids = [(5)]

    @api.model
    def _validahacienda(self, max_invoices=10):  # cron
        invoices = self.env['account.invoice'].search([('type', 'in', ('out_invoice','out_refund')),
                                                       ('state', 'in', ('open', 'paid')),
                                                       ('number_electronic', '!=', False),
                                                       ('date_invoice', '>=', '2018-10-01'),
                                                       '|', ('state_tributacion', '=', False), ('state_tributacion', 'in', ('ne', 'error'))],
                                                      order='number',
                                                      limit=max_invoices)

        total_invoices = len(invoices)
        current_invoice = 0
        _logger.info('MAB - Valida Hacienda - Invoices to check: %s', total_invoices)

        for inv in invoices:
            current_invoice += 1
            _logger.info('MAB - Valida Hacienda - Invoice %s / %s  -  number:%s', current_invoice, total_invoices,
                         inv.number_electronic)

            if not inv.number.isdigit():
                _logger.info('MAB - Valida Hacienda - skipped Invoice %s', inv.number)
                inv.state_tributacion = 'na'
                continue

            if not inv.xml_comprobante:
                url = inv.company_id.frm_callback_url

                tipo_documento = ''
                numero_documento_referencia = ''
                fecha_emision_referencia = ''
                codigo_referencia = ''
                razon_referencia = ''
                medio_pago = inv.payment_methods_id.sequence or '01'
                currency = inv.currency_id

                # Es Factura de cliente o nota de débito
                if inv.type == 'out_invoice':
                    if inv.invoice_id and inv.journal_id and inv.journal_id.nd:
                        tipo_documento = 'ND'
                        numero_documento_referencia = inv.invoice_id.number_electronic
                        tipo_documento_referencia = inv.invoice_id.number_electronic[29:31]
                        fecha_emision_referencia = inv.invoice_id.date_issuance
                        codigo_referencia = inv.reference_code_id.code
                        razon_referencia = inv.reference_code_id.name
                    else:
                        tipo_documento = 'FE'
                        tipo_documento_referencia = ''

                # Si es Nota de Crédito
                elif inv.type == 'out_refund':
                    tipo_documento = 'NC'
                    codigo_referencia = inv.reference_code_id.code
                    razon_referencia = inv.reference_code_id.name

                    if inv.invoice_id.number_electronic:
                        numero_documento_referencia = inv.invoice_id.number_electronic
                        tipo_documento_referencia = inv.invoice_id.number_electronic[29:31]
                        fecha_emision_referencia = inv.invoice_id.date_issuance
                    else:
                        numero_documento_referencia = inv.invoice_id \
                                                      and re.sub('[^0-9]+', '', inv.invoice_id.number).rjust(50, '0') \
                                                      or '0000000'

                        tipo_documento_referencia = '99'
                        date_invoice = datetime.datetime.strptime(inv.invoice_id and inv.invoice_id.date_invoice
                                                                  or '2018-08-30', "%Y-%m-%d")

                        fecha_emision_referencia = date_invoice.strftime("%Y-%m-%d") + "T12:00:00-06:00"

                if inv.payment_term_id:
                    sale_conditions = inv.payment_term_id.sale_conditions_id.sequence or '01'
                else:
                    sale_conditions = '01'

                # Validate if invoice currency is the same as the company currency
                if currency.name == self.company_id.currency_id.name:
                    currency_rate = 1
                else:
                    currency_rate = round(1.0 / currency.rate,5)

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
                for inv_line in inv.invoice_line_ids:
                    line_number += 1
                    quantity = inv_line.quantity
                    if not quantity:
                        continue

                    line_taxes = inv_line.invoice_line_tax_ids.compute_all(inv_line.price_unit, currency, 1,
                                                                           product=inv_line.product_id,
                                                                           partner=inv_line.invoice_id.partner_id)

                    price_unit = round(line_taxes['total_excluded'], 5)  #ajustar para IVI

                    base_line = round(price_unit * quantity, 5)
                    descuento = inv_line.discount and round(price_unit * quantity * inv_line.discount / 100.0, 5) or 0.0
                    subtotal_line = round(base_line - descuento, 5)

                    line = {
                        "cantidad": quantity,
                        "detalle": escape(inv_line.name[:159]),
                        "precioUnitario": price_unit,
                        "montoTotal": base_line,
                        "subtotal": subtotal_line,
                    }
                    if inv_line.product_id:
                        line["unidadMedida"] = inv_line.product_id.uom_id.code or 'Sp'
                        line["codigo"] = inv_line.product_id.default_code or ''

                    if inv_line.discount:
                        total_descuento += descuento
                        line["montoDescuento"] = descuento
                        line["naturalezaDescuento"] = 'Descuento Comercial'

                    # Se generan los impuestos
                    taxes = dict()
                    impuesto_linea = 0.0
                    if inv_line.invoice_line_tax_ids:
                        tax_index = 0

                        taxes_lookup = {}
                        for i in inv_line.invoice_line_tax_ids:
                            taxes_lookup[i.id] = {'tax_code': i.tax_code, 'tarifa': i.amount}
                        for i in line_taxes['taxes']:
                            if taxes_lookup[i['id']]['tax_code'] != '00':
                                tax_index += 1
                                tax_amount = round(subtotal_line*taxes_lookup[i['id']]['tarifa']/100, 5)
                                impuesto_linea += tax_amount
                                tax = {
                                    'codigo': taxes_lookup[i['id']]['tax_code'],
                                    'tarifa': taxes_lookup[i['id']]['tarifa'],
                                    'monto': tax_amount,
                                }
                                # Se genera la exoneración si existe para este impuesto
                                if inv_line.exoneration_id:
                                    tax["exoneracion"] = {
                                        "tipoDocumento": inv_line.exoneration_id.type,
                                        "numeroDocumento": inv_line.exoneration_id.exoneration_number,
                                        "nombreInstitucion": inv_line.exoneration_id.name_institution,
                                        "fechaEmision": str(inv_line.exoneration_id.date) + 'T00:00:00-06:00',
                                        "montoImpuesto": round(tax_amount * inv_line.exoneration_id.percentage_exoneration / 100, 2),
                                        "porcentajeCompra": int(inv_line.exoneration_id.percentage_exoneration)
                                    }

                                taxes[tax_index] = tax

                    line["impuesto"] = taxes

                    # Si no hay product_id se asume como mercaderia
                    if inv_line.product_id and inv_line.product_id.type == 'service':
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

                    line["montoTotalLinea"] = subtotal_line + impuesto_linea

                    lines[line_number] = line

                response_json = functions.make_xml_invoice(inv, tipo_documento, inv.number, inv.date_issuance,
                                                           sale_conditions, medio_pago, total_servicio_gravado,
                                                           total_servicio_exento, total_mercaderia_gravado,
                                                           total_mercaderia_exento, base_subtotal,
                                                           total_impuestos, total_descuento, json.dumps(lines, ensure_ascii=False),
                                                           tipo_documento_referencia, numero_documento_referencia,
                                                           fecha_emision_referencia,
                                                           codigo_referencia, razon_referencia, url, currency_rate)
                if response_json['status'] != 200:
                    _logger.error('MAB - API Error creating XML:%s', response_json['text'])
                    inv.state_tributacion = 'error'
                    continue

                xml = response_json.get('xml')
                response_json = functions.sign_xml(inv, tipo_documento, url, xml)

                if response_json['status'] != 200:
                    _logger.error('MAB - API Error signing XML:%s', response_json['text'])
                    inv.state_tributacion = 'error'
                    continue

                inv.fname_xml_comprobante = tipo_documento + '_' + inv.number_electronic + '.xml'
                inv.xml_comprobante = response_json.get('xmlFirmado')
                _logger.info('MAB - SIGNED XML:%s', inv.fname_xml_comprobante)

            # get token
            response_json = functions.token_hacienda(inv.company_id)
            if response_json['status'] == 200:
                response_json = functions.send_file(inv, response_json['token'], inv.xml_comprobante, inv.company_id.frm_ws_ambiente)
                response_status = response_json.get('status')
                response_text = response_json.get('text')
                if 200 <=  response_status <= 299:
                    inv.state_tributacion = 'procesando'
                else:
                    if response_text.find('ya fue recibido anteriormente') != -1:
                        inv.state_tributacion = 'procesando'
                        inv.message_post(subject='Error', body='Ya recibido anteriormente, se pasa a consultar')
                    elif inv.error_count > 10:
                        inv.message_post(subject='Error', body=response_text)
                        inv.state_tributacion = 'error'
                        _logger.error('MAB - Invoice: %s  Status: %s Error sending XML: %s', inv.number_electronic,
                                      response_status, response_text)
                    else:
                        inv.error_count += 1
                        inv.state_tributacion = 'procesando'
                        inv.message_post(subject='Error', body=response_text)
                        _logger.error('MAB - Invoice: %s  Status: %s Error sending XML: %s', inv.number_electronic,
                                      response_status, response_text)
            else:
                _logger.error('MAB - Error obteniendo token_hacienda')
        _logger.info('MAB - Valida Hacienda - Finalizado Exitosamente')

    @api.multi
    def action_invoice_open(self):
        super(AccountInvoiceElectronic, self).action_invoice_open()
        
        # Revisamos si el ambiente para Hacienda está habilitado
        if self.company_id.frm_ws_ambiente != 'disabled':
            url = self.company_id.frm_callback_url
            now_utc = datetime.datetime.now(pytz.timezone('UTC'))
            now_cr = now_utc.astimezone(pytz.timezone('America/Costa_Rica'))
            date_cr = now_cr.strftime("%Y-%m-%dT%H:%M:%S-06:00")

            for inv in self:
                if inv.journal_id.type == 'sale':
                    if inv.number.isdigit() and (len(inv.number) == 10):
                        tipo_documento = ''
                        next_number = inv.number
                        currency = inv.currency_id

                        # Es Factura de cliente
                        if inv.type == 'out_invoice':

                            # Verificar si es nota DEBITO
                            if inv.invoice_id and inv.journal_id and inv.journal_id.nd:
                                tipo_documento = 'ND'

                            else:
                                tipo_documento = 'FE'

                        # Si es Nota de Crédito
                        elif inv.type == 'out_refund':
                            tipo_documento = 'NC'

                        # tipo de identificación
                        if not self.company_id.identification_id:
                            raise UserError(
                                'Seleccione el tipo de identificación del emisor en el perfil de la compañía')

                        # identificación
                        if inv.partner_id and inv.partner_id.vat:
                            identificacion = re.sub('[^0-9]', '', inv.partner_id.vat)
                            id_code = inv.partner_id.identification_id and inv.partner_id.identification_id.code
                            if not id_code:
                                if len(identificacion) == 9:
                                    id_code = '01'
                                elif len(identificacion) == 10:
                                    id_code = '02'
                                elif len(identificacion) in (11, 12):
                                    id_code = '03'
                                else:
                                    id_code = '05'

                            if id_code == '01' and len(identificacion) != 9:
                                raise UserError('La Cédula Física del emisor debe de tener 9 dígitos')
                            elif id_code == '02' and len(identificacion) != 10:
                                raise UserError('La Cédula Jurídica del emisor debe de tener 10 dígitos')
                            elif id_code == '03' and len(identificacion) not in (11, 12):
                                raise UserError('La identificación DIMEX del emisor debe de tener 11 o 12 dígitos')
                            elif id_code == '04' and len(identificacion) != 10:
                                raise UserError('La identificación NITE del emisor debe de tener 10 dígitos')

                        if inv.payment_term_id and not inv.payment_term_id.sale_conditions_id:
                            raise UserError('No se pudo Crear la factura electrónica: \n Debe configurar condiciones de pago para' +
                                    inv.payment_term_id.name)

                        # Validate if invoice currency is the same as the company currency
                        if currency.name != self.company_id.currency_id.name and (not currency.rate_ids or not (len(currency.rate_ids) > 0)):
                            raise UserError('No hay tipo de cambio registrado para la moneda ' + currency.name)

                        # Generando la clave como la especifica Hacienda
                        response_json = functions.get_clave(self, tipo_documento, next_number, inv.journal_id.sucursal,
                                                            inv.journal_id.terminal)

                        inv.date_issuance = date_cr
                        inv.number_electronic = response_json.get('clave')
                        inv.number = response_json.get('consecutivo')
                    else:
                        raise UserError('Debe configurar correctamente la secuencia del documento')


