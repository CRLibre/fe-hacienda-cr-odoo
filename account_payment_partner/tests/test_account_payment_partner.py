# Copyright 2017 Eficent Business and IT Consulting Services S.L.
# License AGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import fields, _
from odoo.tests import common
from odoo.exceptions import ValidationError


class TestAccountPaymentPartner(common.SavepointCase):

    @classmethod
    def setUpClass(cls):
        super(TestAccountPaymentPartner, cls).setUpClass()

        cls.res_users_model = cls.env['res.users']
        cls.journal_model = cls.env['account.journal']
        cls.payment_mode_model = cls.env['account.payment.mode']
        cls.partner_bank_model = cls.env['res.partner.bank']

        # Refs
        cls.company = cls.env.ref('base.main_company')
        cls.acct_type_payable = cls.env.ref(
            'account.data_account_type_payable')
        cls.acct_type_expenses = cls.env.ref(
            'account.data_account_type_expenses')

        cls.company_2 = cls.env['res.company'].create(
            {'name': 'Company 2'},
        )
        charts = cls.env['account.chart.template'].search([])
        if charts:
            cls.chart = charts[0]
        else:
            raise ValidationError(
                _("No Chart of Account Template has been defined !"))
        cls.wizard = cls.env['wizard.multi.charts.accounts'].create({
            'company_id': cls.company_2.id,
            'chart_template_id': cls.chart.id,
            'sale_tax_id': False,
            'purchase_tax_id': False,
            'code_digits': 6,
            'currency_id': cls.env.ref('base.EUR').id,
            'transfer_account_id': cls.chart.transfer_account_id.id,
        })
        cls.wizard.execute()

        # refs
        cls.manual_out = cls.env.ref(
            'account.account_payment_method_manual_out')
        cls.manual_in = cls.env.ref(
            'account.account_payment_method_manual_in')

        cls.journal_sale = cls.env['account.journal'].create({
            'name': 'Test Sales Journal',
            'code': 'tSAL',
            'type': 'sale',
            'company_id': cls.company.id,
        })

        cls.journal_c1 = cls.journal_model.create({
            'name': 'J1',
            'code': 'J1',
            'type': 'bank',
            'company_id': cls.company.id,
            'bank_acc_number': '123456',
        })

        cls.journal_c2 = cls.journal_model.create({
            'name': 'J2',
            'code': 'J2',
            'type': 'bank',
            'company_id': cls.company_2.id,
            'bank_acc_number': '552344',
        })

        cls.supplier_payment_mode = cls.payment_mode_model.create({
            'name': 'Suppliers Bank 1',
            'bank_account_link': 'variable',
            'payment_method_id': cls.manual_out.id,
            'company_id': cls.company.id,
            'fixed_journal_id': cls.journal_c1.id,
            'variable_journal_ids': [(6, 0, [cls.journal_c1.id])]
        })

        cls.supplier_payment_mode_c2 = cls.payment_mode_model.create({
            'name': 'Suppliers Bank 2',
            'bank_account_link': 'variable',
            'payment_method_id': cls.manual_out.id,
            'company_id': cls.company_2.id,
            'fixed_journal_id': cls.journal_c2.id,
            'variable_journal_ids': [(6, 0, [cls.journal_c2.id])]
        })

        cls.customer_payment_mode = cls.payment_mode_model.create({
            'name': 'Customers to Bank 1',
            'bank_account_link': 'fixed',
            'payment_method_id': cls.manual_in.id,
            'company_id': cls.company.id,
            'fixed_journal_id': cls.journal_c1.id,
            'variable_journal_ids': [(6, 0, [cls.journal_c1.id])]
        })

        cls.customer = cls.env['res.partner'].with_context(
            force_company=cls.company.id).create({
                'name': 'Test customer',
                'customer_payment_mode_id': cls.customer_payment_mode,
            })

        cls.supplier = cls.env['res.partner'].with_context(
            force_company=cls.company.id).create({
                'name': 'Test supplier',
                'supplier_payment_mode_id': cls.supplier_payment_mode,
                'bank_ids': [
                    (0, 0, {
                        'acc_number': '5345345',
                        'company_id': cls.company.id,
                    }),
                    (0, 0, {
                        'acc_number': '3452342',
                        'company_id': cls.company_2.id,
                    })]
            })
        cls.supplier.with_context(
            force_company=cls.company_2.id).supplier_payment_mode_id = \
            cls.supplier_payment_mode_c2

        cls.invoice_account = cls.env['account.account'].search(
            [('user_type_id', '=', cls.acct_type_payable.id),
             ('company_id', '=', cls.company.id)],
            limit=1)
        cls.invoice_line_account = cls.env['account.account'].search(
            [('user_type_id', '=', cls.acct_type_expenses.id),
             ('company_id', '=', cls.company.id)],
            limit=1)

    def _create_invoice(self):

        invoice = self.env['account.invoice'].create({
            'partner_id': self.supplier.id,
            'journal_id': self.journal_sale.id,
            'account_id': self.invoice_account.id,
            'type': 'in_invoice',
            'company_id': self.company.id,
            'payment_mode_id': self.env.ref(
                'account_payment_mode.payment_mode_outbound_ct1').id
        })

        self.env['account.invoice.line'].create({
            'product_id': self.env.ref('product.product_product_4').id,
            'quantity': 1.0,
            'price_unit': 100.0,
            'invoice_id': invoice.id,
            'name': 'product that cost 100',
            'account_id': self.invoice_line_account.id,
        })
        return invoice

    def test_create_partner(self):
        customer = self.env['res.partner'].with_context(
            force_company=self.company.id).create({
                'name': 'Test customer',
                'customer_payment_mode_id': self.customer_payment_mode,
            })

        self.assertEquals(customer.with_context(
            force_company=self.company.id).customer_payment_mode_id,
            self.customer_payment_mode)
        self.assertEquals(customer.with_context(
            force_company=self.company_2.id).customer_payment_mode_id,
            self.payment_mode_model)

    def test_out_invoice_onchange(self):
        # Test the onchange methods in invoice
        invoice = self.env['account.invoice'].new({
            'partner_id': self.customer.id,
            'type': 'out_invoice',
            'company_id': self.company.id,
        })

        invoice._onchange_partner_id()

        self.assertEquals(invoice.payment_mode_id, self.customer_payment_mode)
        self.assertEquals(
            invoice.partner_bank_id,
            self.customer_payment_mode.fixed_journal_id.bank_account_id)

        invoice.company_id = self.company_2
        invoice._onchange_partner_id()
        self.assertEquals(invoice.payment_mode_id, self.payment_mode_model)

        invoice.payment_mode_id = False
        invoice._onchange_payment_mode_id()
        self.assertFalse(invoice.partner_bank_id)

    def test_in_invoice_onchange(self):
        # Test the onchange methods in invoice
        self.manual_out.bank_account_required = True
        invoice = self.env['account.invoice'].new({
            'partner_id': self.supplier.id,
            'type': 'in_invoice',
            'company_id': self.company.id,
        })

        invoice._onchange_partner_id()

        self.assertEquals(invoice.payment_mode_id, self.supplier_payment_mode)
        bank = self.partner_bank_model.search(
            [('acc_number', '=', '5345345')], limit=1)
        self.assertEquals(
            invoice.partner_bank_id,
            bank)

        invoice.company_id = self.company_2
        invoice._onchange_partner_id()
        self.assertEquals(invoice.payment_mode_id,
                          self.supplier_payment_mode_c2)
        bank = self.partner_bank_model.search(
            [('acc_number', '=', '3452342')], limit=1)
        self.assertEquals(invoice.partner_bank_id, bank)

        invoice.payment_mode_id = self.supplier_payment_mode
        invoice._onchange_payment_mode_id()
        self.assertTrue(invoice.partner_bank_id)

        self.manual_out.bank_account_required = False

        invoice.payment_mode_id = self.supplier_payment_mode_c2
        invoice._onchange_payment_mode_id()
        self.assertFalse(invoice.partner_bank_id)

        invoice.partner_id = False
        invoice._onchange_partner_id()
        self.assertEquals(invoice.payment_mode_id,
                          self.payment_mode_model)
        self.assertEquals(invoice.partner_bank_id,
                          self.partner_bank_model)

    def test_invoice_create(self):
        invoice = self._create_invoice()
        invoice.action_invoice_open()
        aml = invoice.move_id.line_ids.filtered(
            lambda l: l.account_id.user_type_id == self.acct_type_payable)
        self.assertEquals(invoice.payment_mode_id,
                          aml[0].payment_mode_id)

    def test_invoice_constrains(self):
        with self.assertRaises(ValidationError):
            self.env['account.invoice'].create({
                'partner_id': self.supplier.id,
                'type': 'in_invoice',
                'company_id': self.company.id,
                'payment_mode_id': self.supplier_payment_mode_c2.id
            })

    def test_payment_mode_constrains_01(self):
        self.env['account.invoice'].create({
            'partner_id': self.supplier.id,
            'type': 'in_invoice',
            'company_id': self.company.id,
        })
        with self.assertRaises(ValidationError):
            self.supplier_payment_mode.company_id = self.company_2

    def test_payment_mode_constrains_02(self):
        self.env['account.move'].create({
            'date': fields.Date.today(),
            'journal_id': self.journal_sale.id,
            'name': '/',
            'ref': 'reference',
            'state': 'draft',
            'line_ids': [(0, 0, {
                'account_id': self.invoice_account.id,
                'credit': 1000,
                'debit': 0,
                'name': 'Test',
                'ref': 'reference',
            }), (0, 0, {
                'account_id': self.invoice_line_account.id,
                'credit': 0,
                'debit': 1000,
                'name': 'Test',
                'ref': 'reference',
            })]})
        with self.assertRaises(ValidationError):
            self.supplier_payment_mode.company_id = self.company_2

    def test_invoice_refund(self):
        invoice = self._create_invoice()
        invoice.action_invoice_open()
        # Lets create a refund invoice for invoice_1.
        # I refund the invoice Using Refund Button.
        context = {"active_model": 'account.invoice',
                   "active_ids": [invoice.id], "active_id": invoice.id}
        account_invoice_refund = self.env[
            'account.invoice.refund'].with_context(context).create(dict(
                description='Refund for Invoice',
                filter_refund='refund',
            ))
        # I clicked on refund button.
        account_invoice_refund.with_context(context).invoice_refund()
        invoice_refund = invoice.refund_invoice_ids[0]

        self.assertEquals(invoice_refund.payment_mode_id,
                          invoice.payment_mode_id)
        self.assertEquals(invoice_refund.partner_bank_id,
                          invoice.partner_bank_id)

    def test_partner(self):
        self.customer.write({
            'customer_payment_mode_id': self.customer_payment_mode.id
        })
        self.assertEqual(
            self.customer.customer_payment_mode_id,
            self.customer_payment_mode
        )
