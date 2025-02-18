# Copyright 2013 Akretion (http://www.akretion.com)
# Copyright 2021 Camptocamp (http://www.camptocamp.com)
# Simone Orsi <simone.orsi@camptocamp.com>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class SeBinding(models.AbstractModel):
    _name = "se.binding"
    _description = "Search Engine Binding"

    # Tech flag to identify model for SE bindings
    _se_model = True
    # Tech flag to identify model for SE bindings
    # that do not require lang specific indexes.
    # This flag does not trigger any automatic machinery.
    # It provides a common key to provide implementers a unified way
    # to check whether their specific binding models need or not lang spec index.
    _se_index_lang_agnostic = False

    se_backend_id = fields.Many2one(
        "se.backend", related="index_id.backend_id", string="Search Engine Backend"
    )
    index_id = fields.Many2one(
        "se.index",
        string="Index",
        required=True,
        # TODO: shall we use 'restrict' here to preserve existing data?
        ondelete="cascade",
    )
    sync_state = fields.Selection(
        [
            ("new", "New"),
            ("to_update", "To update"),
            ("scheduled", "Scheduled"),
            ("done", "Done"),
            ("to_be_checked", "To be checked"),
        ],
        default="new",
        readonly=True,
    )
    date_modified = fields.Date(readonly=True)
    date_syncronized = fields.Date(readonly=True)
    data = fields.Serialized()
    data_display = fields.Text(
        compute="_compute_data_display",
        help="Include this in debug mode to be able to inspect index data.",
    )
    active = fields.Boolean(string="Active", default=True)

    @api.depends("data")
    def _compute_data_display(self):
        for rec in self:
            rec.data_display = json.dumps(rec.data, sort_keys=True, indent=4)

    def get_export_data(self):
        """Public method to retrieve export data."""
        return self.data

    @api.model
    def create(self, vals):
        record = super(SeBinding, self).create(vals)
        record.jobify_recompute_json()
        return record

    def write(self, vals):
        not_new = self.browse()
        if "active" in vals and not vals["active"]:
            not_new = self.filtered(lambda x: x.sync_state != "new")
            new_vals = vals.copy()
            new_vals["sync_state"] = "to_update"
            super(SeBinding, not_new).write(new_vals)

        res = super(SeBinding, self - not_new).write(vals)
        return res

    def unlink(self):
        for record in self:
            if record.sync_state == "new" or (
                record.sync_state == "done" and not record.active
            ):
                continue
            if record.active:
                raise UserError(record._msg_cannot_delete_active())
            else:
                raise UserError(record._msg_cannot_delete_not_synchronized())
        return super(SeBinding, self).unlink()

    def _msg_cannot_delete_active(self):
        return (
            _("You cannot delete the binding '%s', unactivate it first.")
            % self.display_name
        )

    def _msg_cannot_delete_not_synchronized(self):
        return (
            _("You cannot delete the binding '%s', wait until it's synchronized.")
            % self.display_name
        )

    def jobify_recompute_json(self, force_export=False):
        description = _("Recompute %s json and check if need update" % self._name)
        # The job creation with tracking is very costly. So disable it.
        for record in self.with_context(tracking_disable=True):
            record.with_delay(description=description).recompute_json(
                force_export=force_export
            )

    def _work_by_index(self, active=True):
        self = self.exists()
        for backend in self.mapped("se_backend_id"):
            for index in self.mapped("index_id"):
                bindings = self.filtered(
                    lambda b, backend=backend, index=index: b.se_backend_id == backend
                    and b.index_id == index
                    and b.active == active
                )
                specific_backend = backend.specific_backend
                with specific_backend.work_on(
                    self._name, records=bindings, index=index
                ) as work:
                    yield work

    # TODO maybe we need to add lock (todo check)
    def recompute_json(self, force_export=False):
        """Compute index record data as JSON."""
        # `sudo` because the recomputation can be triggered from everywhere
        # (eg: an update of a product in the stock) and is not granted
        # that the user triggering it has access to all required records
        # (eg: se.backend or related records needed to compute index values).
        # All in all, this is safe because the index data should always
        # be the same no matter the access rights of the user triggering this.
        result = []
        validation_errors = []
        to_be_checked = []
        for work in self.sudo()._work_by_index():
            mapper = work.component(usage="se.export.mapper")
            for binding in work.records.with_context(
                **self._recompute_json_work_ctx(work)
            ):
                index_record = mapper.map_record(binding).values()
                # Validate data and track items to check
                error = self._validate_record(work, index_record)
                if error:
                    msg = "{}: {}".format(str(binding), error)
                    _logger.error(msg)
                    validation_errors.append(msg)
                    to_be_checked.append(binding.id)
                    # skip record
                    continue
                if binding.data != index_record or force_export:
                    vals = {"data": index_record}
                    if binding.sync_state != "to_update":
                        vals["sync_state"] = "to_update"
                    binding.write(vals)
        if validation_errors:
            result.append(_("Validation errors") + "\n" + "\n".join(validation_errors))
        if to_be_checked:
            self.browse(to_be_checked).write({"sync_state": "to_be_checked"})
        return "\n\n".join(result)

    def _recompute_json_work_ctx(self, work):
        ctx = {}
        if work.index.lang_id:
            ctx["lang"] = work.index.lang_id.code
        return ctx

    def _validate_record(self, work, index_record):
        return work.collection._validate_record(index_record)

    def synchronize(self):
        # We volontary do the export and delete in the same transaction
        # we try first to process it into two different process but the code
        # was more complex and it was harder to catch/understand
        # active/inactive case for example:
        #
        # 1. some body bind a product and an export job is created
        # 2. the binding is inactivated
        # 3. when the job runs we must exclude all inactive binding
        #
        # Hence in both export/delete we have to re-filter all bindings
        # using one transaction and one sync method allow to filter only once
        # and to do the right action as we are in a transaction.
        export_ids = []
        delete_ids = []
        for work in self.sudo()._work_by_index():
            exporter = work.component(usage="se.record.exporter")
            exporter.run()
            export_ids += work.records.ids
        for work in self.sudo()._work_by_index(active=False):
            deleter = work.component(usage="record.exporter.deleter")
            deleter.run()
            delete_ids += work.records.ids
        return "Exported ids : {}\nDeleted ids : {}".format(export_ids, delete_ids)
