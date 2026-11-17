/** Node:{id,name,role,status,lineage,position,delay_ms}; RequestSet:{id,code,title,events,note}; Trial:{id,name,request_set_id,max_delay_ms,sticky_ms,input_snapshot,status,result,error}
 *  RetentionTemplate:{id,code,title,space_quota_bytes,write_size_bytes,initial_primary_position,initial_segment_size,space_policy,replicas,events,note}
 *  RetentionDrill:{id,name,retention_template_id,status,input_snapshot,result,error}；result 含 trace/final_segments/final_replicas/cleanup_tasks/auto_reclaims/summary/checksum */
export const entityNames=['nodes','request_sets','trials','retention_templates','retention_drills'];
