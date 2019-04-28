from odoo import models, fields, api


class ReferenceCode(models.Model):
    _name = "reference.code"

    active = fields.Boolean(string="Activo", required=False, default=True)
    code = fields.Char(string="Código", required=False, )
    name = fields.Char(string="Nombre", required=False, )
