import java.util.Map;
import kotlin.Pair;
import com.molo17.gluesync.commons.model.api.MappingFunctionOperation;
import org.slf4j.Logger;

/* 
 * That's a Gluesync UDF
 * Gluesync UDFs are custom functions that are used to perform custom operations on the data being processed by Gluesync.
 * 
 * The function is invoked by Gluesync whatever the operation is (INSERT, UPDATE, DELETE), as well as while performing the Snapshot task.
 * The function is invoked with the following parameters:
 * - newValues: the new values of the row
 * - oldValues: the old values of the row (for UPDATE and DELETE operations)
 * - operation: the operation performed on the row (INSERT, UPDATE, DELETE), operation is an enum that can be Insert, Update, Delete
 * 
 * The function should return a Pair of the following values:
 * - the operation performed on the row (INSERT, UPDATE, DELETE): this is the operation that will be performed on the target table, it can be different from the operation performed on the source table
 * - the new values of the row: this is the new values of the row that will be inserted or updated on the target table
 *
 * You can also hook into the logger object to log messages, such as .debug(), .info(), or .error(). 
 */

public class UDF_fd84c12ae981451693ff25e33f6a7f00 {

    public Pair<MappingFunctionOperation, Map<String, Object>> onChange(Map<String, Object> newValues, Map<String, Object> oldValues, MappingFunctionOperation operation, Logger logger) {
        // Source columns: user_key, user_account_id, user_display_name, user_email, role_type, custom_field_id, last_sync_at, timezone, customer_id
        // Target columns: emailAddress, name, surname, user_account_id, user_display_name, timezone, customerId, roleType
        
        String fullName = newValues.get("user_display_name").toString();
		if (fullName == null || fullName.trim().isEmpty()) {
        	// empty
   	 }
    
    	String trimmed = fullName.trim();
    
    	int lastSpace = trimmed.lastIndexOf(' ');
    
    	if (lastSpace == -1) {
    	    // Only one word → treat as first name, surname empty
    	    String first = trimmed;
     	   String last  = "";
     	   newValues.put("name", first); 
     	   newValues.put("surname", last);    	   
    	} else {
    	    String first = trimmed.substring(0, lastSpace).trim();
     	   String last  = trimmed.substring(lastSpace + 1).trim();
     	   newValues.put("name", first); 
     	   newValues.put("surname", last);  
    	}
		
 	   newValues.put("emailAddress", newValues.get("user_email"));	
 	   newValues.put("roleType", newValues.get("role_type"));
 	   newValues.put("user_account_id", newValues.get("user_account_id"));	
		newValues.put("timezone", newValues.get("timezone"));
		newValues.put("customerId", newValues.get("customer_id"));
		       
        return new Pair<>(operation, newValues);
    }

}