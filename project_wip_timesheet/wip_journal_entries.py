# © 2019 Numigi (tm) and all its contributors (https://bit.ly/numigiens)
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class ProjectType(models.Model):
    """Add the salary account to project types."""

    _inherit = "project.type"

    salary_journal_id = fields.Many2one(
        "account.journal",
        "Salary Journal",
        company_dependent=True,
        help="Journal used for transfering salaries into work in progress "
        "when creating or updating a timesheet entry.",
    )

    salary_account_id = fields.Many2one(
        "account.account",
        "Salary Account",
        company_dependent=True,
        help="Account used for the salaries (usually the credit part) "
        "when transfering salaries into work in progress.",
    )

    @api.constrains("salary_account_id", "salary_journal_id", "wip_account_id")
    def _check_required_fields_for_salary_entries(self):
        project_types_with_salary_account = self.filtered(lambda t: t.salary_account_id)
        for project_type in project_types_with_salary_account:
            if not project_type.wip_account_id:
                raise ValidationError(
                    _(
                        "If the salary account is filled for a project type, "
                        "the work in progress account must be filled as well."
                    )
                )

            if not project_type.salary_journal_id:
                raise ValidationError(
                    _(
                        "If the salary account is filled for a project type, "
                        "the salary journal must be filled as well."
                    )
                )


def _format_timesheet_line_description(timesheet_line):
    task = _("(task: {})").format(timesheet_line.task_id.id)
    return "{} {}".format(timesheet_line.name, task) if timesheet_line.name else task


class TimesheetLine(models.Model):

    _inherit = "account.analytic.line"

    salary_account_move_id = fields.Many2one(
        "account.move", "Salary Journal Entry", ondelete="restrict"
    )

    def _get_salary_journal(self):
        """Get the journal to use for wip entry.

        :rtype: account.journal
        """
        return self.project_id.project_type_id.salary_journal_id

    def _get_salary_account(self):
        """Get the account to use for the salary move line.

        :rtype: account.account
        """
        return self.project_id.project_type_id.salary_account_id

    def _get_wip_account(self):
        """Get the account to use for the wip move line.

        :rtype: account.account
        """
        return self.project_id.project_type_id.wip_account_id

    def _requires_wip_salary_move(self):
        """Evaluate whether the timesheet line requires a wip journal entry.

        If the account.analytic.line has a value in the field project_id,
        it is a timesheet line.

        If the project type has a salary account, then the line requires
        a salary account move.

        :rtype: bool
        """
        return self.amount and bool(self._get_salary_account())

    def _get_wip_move_line_vals(self):
        """Get the values for the wip account move line (usually the debit).

        :rtype: dict
        """
        return {
            "account_id": self._get_wip_account().id,
            "name": self.name,
            "debit": -self.amount if self.amount < 0 else 0,
            "credit": self.amount if self.amount > 0 else 0,
            "quantity": self.unit_amount,
            "analytic_account_id": self.project_id.analytic_account_id.id,
            "task_id": self.task_id.id,
        }

    def _get_salary_move_line_vals(self):
        """Get the values for the salary account move line (usually the credit).

        :rtype: dict
        """
        return {
            "account_id": self._get_salary_account().id,
            "name": self.name,
            "debit": self.amount if self.amount > 0 else 0,
            "credit": -self.amount if self.amount < 0 else 0,
            "quantity": self.unit_amount,
        }

    def _get_wip_account_move_vals(self):
        """Get the values for the wip account move.

        :rtype: dict
        """
        reference = "{project} / TA#{task}".format(
            project=self.project_id.display_name, task=self.task_id.id
        )
        return {
            "company_id": self.company_id.id,
            "journal_id": self._get_salary_journal().id,
            "date": self.date,
            "no_analytic_lines": True,
            "ref": reference,
            "line_ids": [
                (5, 0),
                (0, 0, self._get_wip_move_line_vals()),
                (0, 0, self._get_salary_move_line_vals()),
            ],
        }

    def _create_wip_account_move(self):
        """Create the wip journal entry."""
        vals = self._get_wip_account_move_vals()
        self.salary_account_move_id = self.env["account.move"].create(vals)
        self.salary_account_move_id.post()

    def _is_wip_account_move_reconciled(self):
        """Evaluate whether the wip journal entry is reconciled or not.

        :rtype: bool
        """
        return any(l.full_reconcile_id for l in self.salary_account_move_id.line_ids)

    def _update_wip_account_move(self):
        """Update the wip journal entry."""
        if self._is_wip_account_move_reconciled():
            raise ValidationError(
                _(
                    "The timesheet line {description} can not "
                    "be updated because the work in progress entry ({move_name}) is already "
                    "transfered into the cost of goods sold."
                ).format(
                    description=_format_timesheet_line_description(self),
                    move_name=self.salary_account_move_id.name,
                )
            )

        self.salary_account_move_id.state = "draft"
        vals = self._get_wip_account_move_vals()
        self.salary_account_move_id.write(vals)
        self.salary_account_move_id.post()

    def _reverse_salary_account_move_for_updated_timesheet(self):
        """Reverse the wip journal entry in the context of an updated timesheet."""
        if self._is_wip_account_move_reconciled():
            raise ValidationError(
                _(
                    "The timesheet line {description} can not "
                    "be updated because the work in progress entry ({move_name}) would be "
                    "reversed. This journal entry was already transfered into "
                    "the cost of goods sold."
                ).format(
                    description=_format_timesheet_line_description(self),
                    move_name=self.salary_account_move_id.name,
                )
            )
        self.salary_account_move_id.reverse_moves()
        self.salary_account_move_id = False

    def _create_update_or_reverse_wip_account_move(self):
        """Create / Update / Reverse the wip account move.

        Depending on the status of the timesheet line,
        the wip move is either created, updated or reversed.
        """
        must_create_salary_move = (
            self._requires_wip_salary_move() and not self.salary_account_move_id
        )
        must_update_salary_move = (
            self._requires_wip_salary_move() and self.salary_account_move_id
        )
        must_reverse_salary_move = (
            not self._requires_wip_salary_move() and self.salary_account_move_id
        )

        if must_create_salary_move:
            self._create_wip_account_move()

        elif must_update_salary_move:
            self._update_wip_account_move()

        elif must_reverse_salary_move:
            self._reverse_salary_account_move_for_updated_timesheet()

    @api.model
    def create(self, vals):
        """On timesheet create, create or update the wip journal entry.

        Because the creation of a timesheet is complex, the journal entry
        may be created by a write before the return of super().create(vals).
        """
        line = super().create(vals)
        if line._requires_wip_salary_move():
            line.sudo()._create_update_or_reverse_wip_account_move()
        return line

    def _get_salary_move_dependent_fields(self):
        """Get the fields that trigger an update of the wip entry.

        :rtype: Set
        """
        return {"name", "amount", "unit_amount", "date", "project_id", "task_id"}

    @api.multi
    def write(self, vals):
        """When updating an analytic line, create / update / delete the wip entry.

        Whether the wip entry must be created / updated / deleted depends
        on which field is written to. This prevents an infinite loop.
        """
        super().write(vals)

        fields_to_check = self._get_salary_move_dependent_fields()
        if fields_to_check.intersection(vals):
            for line in self:
                line.sudo()._create_update_or_reverse_wip_account_move()

        return True

    def _reverse_salary_account_move_for_deleted_timesheet(self):
        """Reverse the wip journal entry in the context of a deleted timesheet."""
        if self._is_wip_account_move_reconciled():
            raise ValidationError(
                _(
                    "The timesheet line {description} can not "
                    "be deleted because the work in progress entry ({move_name}) is already "
                    "transfered into the cost of goods sold."
                ).format(
                    description=_format_timesheet_line_description(self),
                    move_name=self.salary_account_move_id.name,
                )
            )
        self.salary_account_move_id.reverse_moves()

    @api.multi
    def unlink(self):
        """Reverse the salary account move entry when a timesheet line is deleted."""
        lines_with_moves = self.filtered(lambda l: l.salary_account_move_id)
        for line in lines_with_moves:
            line.sudo()._reverse_salary_account_move_for_deleted_timesheet()
        return super().unlink()
