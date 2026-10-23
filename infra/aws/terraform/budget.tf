# A monthly budget alert. It only emails budget_emails; it never stops or caps anything. AWS Budgets
# cannot see per-stack spend until the `app` tag is activated as a cost allocation tag, so it covers the
# whole account: run this stack in an account of its own and the two are the same. Infra is a few
# dollars a month; model calls are the real cost and are capped per task in code
# (cost_cap_usd_per_task in config/pipeline.yml), and they are billed by the model providers, not AWS.

resource "aws_budgets_budget" "monthly" {
  count = length(var.budget_emails) > 0 ? 1 : 0

  name         = "${var.stack}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = { actual-50 = [50, "ACTUAL"], actual-90 = [90, "ACTUAL"], actual-100 = [100, "ACTUAL"], forecast-100 = [100, "FORECASTED"] }
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value[0]
      threshold_type             = "PERCENTAGE"
      notification_type          = notification.value[1]
      subscriber_email_addresses = var.budget_emails
    }
  }
}
