# A monthly budget alert for this project. It only emails (the billing account's admins and users);
# it never stops or caps anything. Infra is a few dollars a month; model calls are the real cost and
# are capped per task in code (cost_cap_usd_per_task in config/pipeline.yml), so an alert here means
# something runs far more often than it should.

resource "google_billing_budget" "monthly" {
  count = var.billing_account != "" ? 1 : 0

  billing_account = var.billing_account
  display_name    = "discovery-base monthly"

  budget_filter {
    projects = ["projects/${local.project_number}"]
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.monthly_budget_usd)
    }
  }

  threshold_rules {
    threshold_percent = 0.5
  }
  threshold_rules {
    threshold_percent = 0.9
  }
  threshold_rules {
    threshold_percent = 1.0
  }
  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "FORECASTED_SPEND"
  }

  depends_on = [google_project_service.apis]
}
