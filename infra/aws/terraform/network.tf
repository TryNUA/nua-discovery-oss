# A small VPC of its own for the worker: two public subnets and an internet gateway, nothing else.
#
# The worker needs outbound HTTPS only (ECR, SQS, Secrets Manager, GitHub, Slack, the model APIs) and
# takes no inbound traffic. It runs in a public subnet with a public IP, behind a security group that
# allows no inbound connection at all. A private subnet would need a NAT gateway, about $33 a month
# before data, many times the rest of this stack; VPC endpoints would cost about as much and still not
# reach GitHub or the model APIs. The public IP costs $0.005 an hour, only while the worker runs.

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${var.stack}-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = { Name = "${var.stack}-igw" }
}

resource "aws_subnet" "public" {
  count = 2

  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = { Name = "${var.stack}-public-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${var.stack}-public" }
}

resource "aws_route_table_association" "public" {
  count = 2

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Outbound only: no ingress rule, so nothing can connect to the worker.
resource "aws_security_group" "worker" {
  name        = "${var.stack}-worker"
  description = "nua worker: outbound only"
  vpc_id      = aws_vpc.main.id

  egress {
    description = "HTTPS and git to AWS APIs, GitHub, Slack and the model providers"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.stack}-worker" }
}
