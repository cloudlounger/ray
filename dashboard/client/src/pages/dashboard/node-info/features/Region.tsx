import React from "react";
import { Accessor } from "../../../../common/tableUtils";
import { Typography } from "@material-ui/core";
import {
  ClusterFeatureRenderFn,
  NodeFeatureData,
  NodeFeatureRenderFn,
  NodeInfoFeature,
  WorkerFeatureRenderFn,
} from "./types";

export const ClusterRegion: ClusterFeatureRenderFn = ({ nodes }) => (
  <Typography color="textSecondary" component="span" variant="inherit">
    N/A
  </Typography>
);

export const NodeRegion: NodeFeatureRenderFn = ({ node }) => (
  <React.Fragment>
    {node.region} 
  </React.Fragment>
);

export const nodeRegionAccessor: Accessor<NodeFeatureData> = ({ node }) =>
  node.region;

// Ray worker process titles have one of the following forms: `ray::IDLE`,
// `ray::function()`, `ray::Class`, or `ray::Class.method()`. We extract the
// first portion here for display in the "Host" column. Note that this will
// always be `ray` under the current setup, but it may vary in the future.
export const WorkerRegion: WorkerFeatureRenderFn = ({ worker }) => (
 <Typography color="textSecondary" component="span" variant="inherit">
    N/A
  </Typography>
);

const regionFeature: NodeInfoFeature = {
  id: "region",
  ClusterFeatureRenderFn: ClusterRegion,
  NodeFeatureRenderFn: NodeRegion,
  WorkerFeatureRenderFn: WorkerRegion,
  nodeAccessor: nodeRegionAccessor,
};

export default regionFeature;
