package com.configvault;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.Charset;
import java.nio.charset.CodingErrorAction;
import java.security.SecureRandom;
import java.time.Instant;
import java.util.*;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.core.Authentication;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.web.csrf.CsrfToken;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.annotation.Isolation;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.multipart.MultipartFile;
import org.springframework.web.server.ResponseStatusException;
import static org.springframework.http.HttpStatus.*;

@RestController
@RequestMapping("/api")
public class VaultController {
    private final JdbcTemplate db;
    private final LockMapper locks;
    private final PasswordEncoder encoder;
    private final ObjectMapper mapper;
    public VaultController(JdbcTemplate db, LockMapper locks, PasswordEncoder encoder, ObjectMapper mapper) {
        this.db=db; this.locks=locks; this.encoder=encoder; this.mapper=mapper;
    }
    static ResponseStatusException error(org.springframework.http.HttpStatus status, String msg) { return new ResponseStatusException(status,msg); }
    static Map<String,Object> map(Object... args) {
        Map<String,Object> m=new LinkedHashMap<>();
        for(int i=0;i<args.length;i+=2) m.put((String)args[i],args[i+1]);
        return m;
    }
    long owner(Authentication auth) { return db.queryForObject("SELECT id FROM app_user WHERE username=? AND enabled=1",Long.class,auth.getName()); }
    static long number(Object n) { return ((Number)n).longValue(); }
    static Long nullable(Object n) { return n==null ? null : number(n); }
    static Long inputId(Object value) {
        if (value == null) return null;
        if (!(value instanceof Integer || value instanceof Long) || ((Number)value).longValue() <= 0)
            throw error(BAD_REQUEST,"版本编号必须是正整数。");
        return ((Number)value).longValue();
    }
    static String text(Object n) { return n==null ? "" : n.toString(); }
    @GetMapping("/csrf") public Map<String,String> csrf(CsrfToken token) { return Map.of("token",token.getToken(),"headerName",token.getHeaderName()); }
    @PostMapping("/register") public ResponseEntity<?> register(@RequestBody Map<String,String> data) {
        String name=data.getOrDefault("username","").trim(), pass=data.getOrDefault("password","");
        if (!name.matches("[A-Za-z0-9_]{3,64}") || pass.length()<10 || pass.getBytes(java.nio.charset.StandardCharsets.UTF_8).length>72) throw error(BAD_REQUEST,"用户名须为 3–64 位字母数字或下划线；密码须为 10–72 字符。");
        try { db.update("INSERT INTO app_user(username,password_hash) VALUES (?,?)", name, encoder.encode(pass)); }
        catch(DataIntegrityViolationException ex) { throw error(CONFLICT,"用户名不可用。"); }
        return ResponseEntity.status(CREATED).build();
    }
    @PostMapping("/tokens") public Map<String,Object> token(Authentication auth,@RequestBody Map<String,String> data) {
        if ("api-token".equals(auth.getDetails())) throw error(FORBIDDEN,"请通过浏览器会话管理令牌。");
        String label=data.getOrDefault("label","").trim();
        if(label.isEmpty()||label.length()>80) throw error(BAD_REQUEST,"令牌名称无效。");
        byte[] random=new byte[32]; new SecureRandom().nextBytes(random);
        String token=Base64.getUrlEncoder().withoutPadding().encodeToString(random);
        db.update("INSERT INTO api_token(owner_id,token_hash,label,expires_at) VALUES (?,?,?,DATE_ADD(CURRENT_TIMESTAMP(6),INTERVAL 90 DAY))",owner(auth),SecurityConfig.hash(token.getBytes(java.nio.charset.StandardCharsets.UTF_8)),label);
        long id=db.queryForObject("SELECT id FROM api_token WHERE token_hash=?",Long.class,SecurityConfig.hash(token.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
        return map("id",id,"token",token,"expiresInDays",90);
    }
    @GetMapping("/tokens") public List<Map<String,Object>> tokens(Authentication auth) {
        if ("api-token".equals(auth.getDetails())) throw error(FORBIDDEN,"请通过浏览器会话管理令牌。");
        return db.queryForList("SELECT id,label,expires_at AS expiresAt,revoked_at AS revokedAt FROM api_token WHERE owner_id=? ORDER BY id DESC",owner(auth));
    }
    @DeleteMapping("/tokens/{id}") public ResponseEntity<?> revoke(Authentication auth,@PathVariable long id) {
        if ("api-token".equals(auth.getDetails())) throw error(FORBIDDEN,"请通过浏览器会话管理令牌。");
        if(db.update("UPDATE api_token SET revoked_at=CURRENT_TIMESTAMP(6) WHERE id=? AND owner_id=?",id,owner(auth))==0) throw error(NOT_FOUND,"资源不存在。");
        return ResponseEntity.noContent().build();
    }
    @PostMapping("/config-repositories") @Transactional(isolation=Isolation.READ_COMMITTED) public Map<String,Object> create(Authentication auth,@RequestBody Map<String,String> data) {
        long user=owner(auth); locks.lockUser(user);
        String name=data.getOrDefault("name","").trim();
        if(name.isEmpty()||name.length()>80) throw error(BAD_REQUEST,"库名称须为 1–80 字符。");
        if(db.queryForObject("SELECT COUNT(*) FROM config_repository WHERE owner_id=?",Integer.class,user)>=20) throw error(CONFLICT,"配置库配额已满。");
        try { db.update("INSERT INTO config_repository(owner_id,name) VALUES (?,?)",user,name); }
        catch(DataIntegrityViolationException e) { throw error(CONFLICT,"配置库名称已存在。"); }
        return repo(user,db.queryForObject("SELECT LAST_INSERT_ID()",Long.class));
    }
    @GetMapping("/config-repositories") public List<Map<String,Object>> repositories(Authentication auth) {
        return db.queryForList("SELECT id,name,game,head_version_id AS headVersionId,head_revision AS headRevision,created_at AS createdAt FROM config_repository WHERE owner_id=? ORDER BY id",owner(auth));
    }
    Map<String,Object> repo(long user,long id) {
        var rows=db.queryForList("SELECT id,name,game,head_version_id AS headVersionId,head_revision AS headRevision,created_at AS createdAt FROM config_repository WHERE id=? AND owner_id=?",id,user);
        if(rows.isEmpty()) throw error(NOT_FOUND,"资源不存在。"); return rows.get(0);
    }
    @GetMapping("/config-repositories/{id}") public Map<String,Object> repository(Authentication a,@PathVariable long id) {return repo(owner(a),id);}
    @PatchMapping("/config-repositories/{id}") @Transactional(isolation=Isolation.READ_COMMITTED)
    public Map<String,Object> rename(Authentication auth,@PathVariable long id,@RequestBody Map<String,String> data) {
        long user=owner(auth); locks.lockUser(user);
        if(locks.lockRepository(id,user)==null) throw error(NOT_FOUND,"资源不存在。");
        String name=data.get("name");
        if(name==null || (name=name.trim()).isEmpty() || name.length()>80) throw error(BAD_REQUEST,"库名称须为 1–80 字符。");
        try { db.update("UPDATE config_repository SET name=? WHERE id=? AND owner_id=?",name,id,user); }
        catch(DataIntegrityViolationException e) { throw error(CONFLICT,"配置库名称已存在。"); }
        return repo(user,id);
    }
    @DeleteMapping("/config-repositories/{id}") @Transactional(isolation=Isolation.READ_COMMITTED)
    public ResponseEntity<?> delete(Authentication auth,@PathVariable long id,@RequestBody Map<String,Object> data) {
        if(!data.containsKey("baseVersionId")) throw error(BAD_REQUEST,"删除时需要明确当前版本。");
        Long base=inputId(data.get("baseVersionId"));
        long user=owner(auth); locks.lockUser(user);
        if(locks.lockRepository(id,user)==null) throw error(NOT_FOUND,"资源不存在。");
        if(!Objects.equals(nullable(repo(user,id).get("headVersionId")),base))
            throw error(CONFLICT,"配置库版本已变化，请重新打开并确认后删除。");
        db.update("DELETE FROM idempotency_record WHERE version_id IN (SELECT id FROM config_version WHERE repository_id=?)",id);
        db.update("DELETE FROM audit_event WHERE repository_id=?",id);
        db.update("UPDATE config_repository SET head_version_id=NULL WHERE id=?",id);
        for(Long versionId : db.queryForList("SELECT id FROM config_version WHERE repository_id=? ORDER BY revision DESC",Long.class,id))
            db.update("DELETE FROM config_version WHERE id=? AND repository_id=?",versionId,id);
        db.update("DELETE FROM config_repository WHERE id=? AND owner_id=?",id,user);
        return ResponseEntity.noContent().build();
    }
    Map<String,Object> version(long user,long repo,long id,boolean content) {
        String cols="v.id,v.repository_id AS repositoryId,v.revision,v.parent_id AS parentId,v.restored_from_id AS restoredFromId,v.sha256,v.byte_size AS byteSize,v.encoding_label AS encodingLabel,v.message,v.created_at AS createdAt"+(content?",v.content_blob AS content":"");
        var rows=db.queryForList("SELECT "+cols+" FROM config_version v JOIN config_repository r ON r.id=v.repository_id WHERE r.owner_id=? AND r.id=? AND v.id=?",user,repo,id);
        if(rows.isEmpty()) throw error(NOT_FOUND,"资源不存在。"); return rows.get(0);
    }
    @GetMapping("/config-repositories/{id}/versions") public List<Map<String,Object>> history(Authentication a,@PathVariable long id,@RequestParam(defaultValue="0") int page,@RequestParam(defaultValue="30") int size) {
        long user=owner(a); repo(user,id); if(page<0||page>10000||size<1||size>100) throw error(BAD_REQUEST,"分页参数无效。");
        return db.queryForList("SELECT id,revision,parent_id AS parentId,restored_from_id AS restoredFromId,sha256,byte_size AS byteSize,encoding_label AS encodingLabel,message,created_at AS createdAt FROM config_version WHERE repository_id=? ORDER BY revision DESC LIMIT ? OFFSET ?",id,size,page*size);
    }
    @GetMapping("/config-repositories/{id}/versions/{vid}/content") public ResponseEntity<byte[]> download(Authentication a,@PathVariable long id,@PathVariable long vid) {
        var v=version(owner(a),id,vid,true); byte[] bytes=(byte[])v.get("content");
        return ResponseEntity.ok().contentType(MediaType.APPLICATION_OCTET_STREAM).header("X-Content-SHA256",text(v.get("sha256"))).header("X-Content-Size",text(v.get("byteSize"))).header(HttpHeaders.CONTENT_DISPOSITION,"attachment; filename=\"config.cfg\"").body(bytes);
    }
    record Submission(Long base,String message,String encoding,byte[] content,Long source) {}
    @PostMapping(value="/config-repositories/{id}/versions", consumes=MediaType.MULTIPART_FORM_DATA_VALUE) @Transactional(isolation=Isolation.READ_COMMITTED)
    public Map<String,Object> upload(Authentication a,@PathVariable long id,@RequestHeader("Idempotency-Key") String key,@RequestParam String baseVersionId,@RequestParam(defaultValue="") String message,@RequestParam String encodingLabel,@RequestPart MultipartFile file) throws Exception {
        Long base=parseBase(baseVersionId); byte[] bytes=file.getBytes();
        return submit(owner(a),id,key,new Submission(base,message,encodingLabel,bytes,null),"upload");
    }
    @PostMapping("/config-repositories/{id}/restore") @Transactional(isolation=Isolation.READ_COMMITTED)
    public Map<String,Object> restore(Authentication a,@PathVariable long id,@RequestHeader("Idempotency-Key") String key,@RequestBody Map<String,Object> data) throws Exception {
        long user=owner(a); Long source=inputId(data.get("sourceVersionId"));
        if(source==null||!data.containsKey("baseVersionId")) throw error(BAD_REQUEST,"需要恢复来源和明确的基线。");
        var old=version(user,id,source,true);
        return submit(user,id,key,new Submission(inputId(data.get("baseVersionId")),text(data.get("message")),text(old.get("encodingLabel")),(byte[])old.get("content"),source),"restore");
    }
    static Long parseBase(String s) {
        if (s.isEmpty()) return null;
        try { long id=Long.parseLong(s); if (id>0) return id; }
        catch(NumberFormatException ignored) { }
        throw error(BAD_REQUEST,"基线无效。");
    }
    Map<String,Object> submit(long user,long id,String key,Submission s,String operation) throws Exception {
        if(!key.matches("[A-Za-z0-9_-]{16,128}")) throw error(BAD_REQUEST,"幂等键无效。");
        if(s.content.length>262144) throw error(PAYLOAD_TOO_LARGE,"文件超过 256 KiB。");
        if(s.message.length()>200||!Set.of("unknown","utf-8","utf-8-bom","gb18030").contains(s.encoding)) throw error(BAD_REQUEST,"版本元数据无效。");
        String digest=SecurityConfig.hash(s.content);
        String requestHash=SecurityConfig.hash(mapper.writeValueAsBytes(Arrays.asList(id,s.base,digest,s.message,s.encoding,s.source)));
        locks.lockUser(user);
        var previous=db.queryForList("SELECT request_hash AS requestHash,version_id AS versionId,no_change AS noChange FROM idempotency_record WHERE owner_id=? AND operation=? AND request_key=?",user,operation,key);
        if(!previous.isEmpty()) {
            var p=previous.get(0); if(!requestHash.equals(p.get("requestHash"))) throw error(CONFLICT,"幂等键已用于不同请求。");
            var v=version(user,id,number(p.get("versionId")),false); v.put("noChange",p.get("noChange")); return v;
        }
        if(locks.lockRepository(id,user)==null) throw error(NOT_FOUND,"资源不存在。");
        var r=repo(user,id); Long head=nullable(r.get("headVersionId")); int rev=((Number)r.get("headRevision")).intValue();
        if(!Objects.equals(head,s.base)) throw error(CONFLICT,"基线已过期，当前 head="+head);
        if(s.source==null && head!=null && digest.equals(version(user,id,head,false).get("sha256"))) {
            db.update("INSERT INTO idempotency_record VALUES (?,?,?,?,?,?)",user,operation,key,requestHash,head,true);
            var v=version(user,id,head,false); v.put("noChange",true); return v;
        }
        if(rev>=100) throw error(CONFLICT,"版本配额已满。");
        db.update("INSERT INTO config_version(repository_id,revision,parent_id,restored_from_id,sha256,byte_size,encoding_label,content_blob,message) VALUES (?,?,?,?,?,?,?,?,?)",id,rev+1,head,s.source,digest,s.content.length,s.encoding,s.content,s.message);
        long vid=db.queryForObject("SELECT LAST_INSERT_ID()",Long.class);
        db.update("UPDATE config_repository SET head_version_id=?,head_revision=? WHERE id=?",vid,rev+1,id);
        db.update("INSERT INTO idempotency_record VALUES (?,?,?,?,?,?)",user,operation,key,requestHash,vid,false);
        db.update("INSERT INTO audit_event(actor_id,action,repository_id,version_id,request_id) VALUES (?,?,?,?,?)",user,operation,id,vid,key);
        var v=version(user,id,vid,false); v.put("noChange",false); return v;
    }
    static String decode(Map<String,Object> v) {
        byte[] b=(byte[])v.get("content"); String label=text(v.get("encodingLabel"));
        if(label.equals("unknown")) throw error(UNPROCESSABLE_ENTITY,"未指定编码，无法比较；仍可下载原始字节。");
        if(label.equals("utf-8-bom")) {
            if(b.length<3 || b[0]!=(byte)0xef || b[1]!=(byte)0xbb || b[2]!=(byte)0xbf) throw error(UNPROCESSABLE_ENTITY,"编码标签与 BOM 不符。");
            b=Arrays.copyOfRange(b,3,b.length);
        } else if(label.equals("utf-8") && b.length>=3 && b[0]==(byte)0xef && b[1]==(byte)0xbb && b[2]==(byte)0xbf) throw error(UNPROCESSABLE_ENTITY,"编码标签与 BOM 不符。");
        try { return Charset.forName(label.equals("gb18030")?"GB18030":"UTF-8").newDecoder().onMalformedInput(CodingErrorAction.REPORT).onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(b)).toString(); }
        catch(CharacterCodingException e) { throw error(UNPROCESSABLE_ENTITY,"编码与内容不匹配；仍可下载原始字节。"); }
    }
    @GetMapping("/config-repositories/{id}/diff") public Map<String,Object> diff(Authentication a,@PathVariable long id,@RequestParam long from,@RequestParam long to) {
        long user=owner(a); String[] x=decode(version(user,id,from,true)).split("\n",-1), y=decode(version(user,id,to,true)).split("\n",-1);
        if((long)x.length*y.length>2_000_000) throw error(UNPROCESSABLE_ENTITY,"行数过多，无法在网页比较；请下载后使用本地工具。");
        int[][] dp=new int[x.length+1][y.length+1];
        for(int i=x.length-1;i>=0;i--) for(int j=y.length-1;j>=0;j--) dp[i][j]=x[i].equals(y[j])?dp[i+1][j+1]+1:Math.max(dp[i+1][j],dp[i][j+1]);
        List<Map<String,String>> lines=new ArrayList<>(); int i=0,j=0;
        while(i<x.length||j<y.length) {
            if(i<x.length&&j<y.length&&x[i].equals(y[j])) { lines.add(Map.of("type","same","text",x[i])); i++; j++; }
            else if(j<y.length&&(i==x.length||dp[i][j+1]>dp[i+1][j])) lines.add(Map.of("type","add","text",y[j++]));
            else lines.add(Map.of("type","remove","text",x[i++]));
        }
        return map("from",from,"to",to,"lines",lines);
    }
}
