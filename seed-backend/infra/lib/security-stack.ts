import * as cdk from 'aws-cdk-lib';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cw_actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as subscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import * as budgets from 'aws-cdk-lib/aws-budgets';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { Construct } from 'constructs';

interface SecurityStackProps extends cdk.StackProps {
  stageName: string;
  apiGateway: apigateway.RestApi;
  lambdaFunction: lambda.Function;
  dynamoTable: dynamodb.Table;
  alertEmail: string;
}

export class SecurityStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: SecurityStackProps) {
    super(scope, id, props);

    // ── SNS Alerts Topic ──
    const alertTopic = new sns.Topic(this, 'AlertTopic', {
      topicName: `seed-alerts-${props.stageName}`,
    });
    if (props.alertEmail) {
      alertTopic.addSubscription(
        new subscriptions.EmailSubscription(props.alertEmail)
      );
    }

    // ── WAF ──
    const webAcl = new wafv2.CfnWebACL(this, 'SeedWaf', {
      name: `seed-waf-${props.stageName}`,
      scope: 'REGIONAL',
      defaultAction: { allow: {} },
      visibilityConfig: {
        sampledRequestsEnabled: true,
        cloudWatchMetricsEnabled: true,
        metricName: `seed-waf-${props.stageName}`,
      },
      rules: [
        {
          name: 'rate-limit',
          priority: 1,
          action: { block: {} },
          statement: {
            rateBasedStatement: {
              limit: 100,
              aggregateKeyType: 'IP',
            },
          },
          visibilityConfig: {
            sampledRequestsEnabled: true,
            cloudWatchMetricsEnabled: true,
            metricName: 'seed-rate-limit',
          },
        },
        {
          name: 'aws-common-rules',
          priority: 2,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: 'AWS',
              name: 'AWSManagedRulesCommonRuleSet',
            },
          },
          visibilityConfig: {
            sampledRequestsEnabled: true,
            cloudWatchMetricsEnabled: true,
            metricName: 'seed-common-rules',
          },
        },
        {
          name: 'aws-bot-control',
          priority: 3,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: 'AWS',
              name: 'AWSManagedRulesBotControlRuleSet',
            },
          },
          visibilityConfig: {
            sampledRequestsEnabled: true,
            cloudWatchMetricsEnabled: true,
            metricName: 'seed-bot-control',
          },
        },
      ],
    });

    // Associate WAF with API Gateway stage
    new wafv2.CfnWebACLAssociation(this, 'WafAssociation', {
      resourceArn: props.apiGateway.deploymentStage.stageArn,
      webAclArn: webAcl.attrArn,
    });

    // ── CloudWatch Alarms ──
    const lambdaConcurrency = new cloudwatch.Alarm(this, 'LambdaConcurrency', {
      alarmName: `seed-lambda-concurrency-${props.stageName}`,
      metric: props.lambdaFunction.metric('ConcurrentExecutions', {
        statistic: 'Maximum',
        period: cdk.Duration.minutes(1),
      }),
      threshold: 50,
      evaluationPeriods: 1,
    });
    lambdaConcurrency.addAlarmAction(new cw_actions.SnsAction(alertTopic));

    const lambdaInvocations = new cloudwatch.Alarm(this, 'LambdaInvocations', {
      alarmName: `seed-lambda-invocations-${props.stageName}`,
      metric: props.lambdaFunction.metric('Invocations', {
        statistic: 'Sum',
        period: cdk.Duration.hours(1),
      }),
      threshold: 10000,
      evaluationPeriods: 1,
    });
    lambdaInvocations.addAlarmAction(new cw_actions.SnsAction(alertTopic));

    const ddbWrites = new cloudwatch.Alarm(this, 'DdbWrites', {
      alarmName: `seed-ddb-wcu-${props.stageName}`,
      metric: props.dynamoTable.metric('ConsumedWriteCapacityUnits', {
        statistic: 'Sum',
        period: cdk.Duration.minutes(1),
      }),
      threshold: 6000, // 100 WCU/sec * 60 sec
      evaluationPeriods: 1,
    });
    ddbWrites.addAlarmAction(new cw_actions.SnsAction(alertTopic));

    // ── AWS Budget — $25 cap (thresholds are % of the $25 limit) ──
    // TODO(killswitch): subscribe a Lambda to alertTopic that disables the
    // API Gateway deployment stage when the 100% ($25) notification fires.
    // Design doc §7.5. Not implemented in this skeleton pass.
    new budgets.CfnBudget(this, 'MonthlyCap', {
      budget: {
        budgetName: `seed-monthly-cap-${props.stageName}`,
        budgetType: 'COST',
        timeUnit: 'MONTHLY',
        budgetLimit: { amount: 25, unit: 'USD' },
      },
      notificationsWithSubscribers: [
        {
          notification: {
            notificationType: 'ACTUAL',
            comparisonOperator: 'GREATER_THAN',
            threshold: 20, // $5
          },
          subscribers: [
            { subscriptionType: 'EMAIL', address: props.alertEmail },
          ],
        },
        {
          notification: {
            notificationType: 'ACTUAL',
            comparisonOperator: 'GREATER_THAN',
            threshold: 60, // $15
          },
          subscribers: [
            { subscriptionType: 'EMAIL', address: props.alertEmail },
          ],
        },
        {
          notification: {
            notificationType: 'ACTUAL',
            comparisonOperator: 'GREATER_THAN',
            threshold: 100, // $25 — triggers kill switch via SNS → Lambda
          },
          subscribers: [
            { subscriptionType: 'SNS', address: alertTopic.topicArn },
          ],
        },
      ],
    });
  }
}
