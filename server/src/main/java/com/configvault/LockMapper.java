package com.configvault;

import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Param;

@Mapper
public interface LockMapper {
    @Select("SELECT id FROM app_user WHERE id = #{id} FOR UPDATE")
    Long lockUser(long id);
    @Select("SELECT id FROM config_repository WHERE id = #{id} AND owner_id = #{owner} FOR UPDATE")
    Long lockRepository(@Param("id") long id, @Param("owner") long owner);
}
