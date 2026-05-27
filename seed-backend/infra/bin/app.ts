#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { SeedApiStack } from '../lib/seed-api-stack';
import { SecurityStack } from '../lib/security-stack';

const app = new cdk.App();

const env = app.node.tryGetContext('env') || 'dev';
const awsEnv = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: 'us-east-1',
};

const apiStack = new SeedApiStack(app, `Seed-Api-${env}`, {
  env: awsEnv,
  stageName: env,
  similarityThresholdAuto: '0.85',
  similarityThresholdMin: '0.60',
  modelS3Key: 'minilm-l6-v2.onnx',
});

new SecurityStack(app, `Seed-Security-${env}`, {
  env: awsEnv,
  stageName: env,
  apiGateway: apiStack.api,
  lambdaFunction: apiStack.lambdaFn,
  dynamoTable: apiStack.table,
  alertEmail: app.node.tryGetContext('alertEmail'),
});
