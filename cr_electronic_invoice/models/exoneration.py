from odoo import models, fields, api


class AutEx(models.Model):
    _name = "aut.ex"

    active = fields.Boolean(string="Activo", required=False, default=True)
    code = fields.Char(string="Código", required=False, )
    name = fields.Char(string="Nombre", required=False, )


class Exoneration(models.Model):
    _name = "exoneration"

    name = fields.Char(string="Nombre", required=False, )
    type = fields.Many2one(comodel_name="aut.ex", string="Tipo Autorizacion/Exoneracion", required=True, )
    exoneration_number = fields.Char(string="Número de exoneración", required=False, )
    name_institution = fields.Char(string="Nombre de institución", required=False, )
    date = fields.Date(string="Fecha", required=False, )
    percentage_exoneration = fields.Float(string="Porcentaje de exoneración", required=False, )
